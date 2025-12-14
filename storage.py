"""
图片存储和清理服务（远程存储版）
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx

from config import (
    IMAGE_CLEANUP_INTERVAL,
    IMAGE_EXPIRE_SECONDS,
    REMOTE_IMAGE_BASE_URL,
    REMOTE_IMAGE_FETCH_PATH,
    REMOTE_IMAGE_UPLOAD_PATH,
)

logger = logging.getLogger(__name__)


@dataclass
class RemoteImageInfo:
    """远程图片信息"""

    id: str
    url: str
    mime_type: str
    expires_at: Optional[float]


class ImageStorage:
    """远程图片存储管理器"""

    def __init__(self) -> None:
        self._images: dict[str, RemoteImageInfo] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @staticmethod
    def _build_url(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(REMOTE_IMAGE_BASE_URL.rstrip("/") + "/", path.lstrip("/"))

    @staticmethod
    def _parse_expires_at(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            # HuggingFace 推送的 ISO8601，兼容 Z 结尾
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.debug("无法解析 expires_at: %s", value)
            return None

    @staticmethod
    def _extract_remote_entry(
        payload: object,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        从接口返回中提取 (url, expires_at, mime_type)

        兼容多种返回结构：
        - dict，直接包含 url / expires_at
        - list，取第一个包含 url 的元素
        """
        candidates: list[dict] = []

        if isinstance(payload, dict):
            candidates.append(payload)
            for key in ("data", "result", "results", "images", "items"):
                nested = payload.get(key)
                if isinstance(nested, list):
                    candidates.extend(
                        [item for item in nested if isinstance(item, dict)]
                    )
                elif isinstance(nested, dict):
                    candidates.append(nested)
        elif isinstance(payload, list):
            candidates.extend([item for item in payload if isinstance(item, dict)])

        for candidate in candidates:
            url = (
                candidate.get("url")
                or candidate.get("image_url")
                or candidate.get("imageUrl")
                or candidate.get("path")
            )
            expires_at = candidate.get("expires_at")
            mime_type = candidate.get("mime_type") or candidate.get("content_type")

            identifier = (
                candidate.get("id")
                or candidate.get("image_id")
                or candidate.get("imageId")
            )

            data_field = candidate.get("data")
            if not url and isinstance(data_field, dict):
                url = (
                    data_field.get("url")
                    or data_field.get("image_url")
                    or data_field.get("imageUrl")
                    or data_field.get("path")
                )
                expires_at = expires_at or data_field.get("expires_at")
                mime_type = (
                    mime_type
                    or data_field.get("mime_type")
                    or data_field.get("content_type")
                )
                if not identifier:
                    identifier = (
                        data_field.get("id")
                        or data_field.get("image_id")
                        or data_field.get("imageId")
                    )

            if url:
                return url, expires_at, mime_type
            if identifier:
                fallback_path = REMOTE_IMAGE_FETCH_PATH.format(id=identifier)
                return fallback_path, expires_at, mime_type

        return None, None, None

    async def save(self, data: str, mime_type: str = "image/png") -> RemoteImageInfo:
        """
        保存 base64 图片数据到远程存储

        Args:
            data: base64 编码的图片数据
            mime_type: MIME 类型


        Returns:

            RemoteImageInfo: 远程图片信息

        """
        upload_url = self._build_url(REMOTE_IMAGE_UPLOAD_PATH)
        payload = {
            "image_base64": data,
            "mime_type": mime_type,
        }

        client = await self._get_client()
        try:
            response = await client.post(upload_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("上传图片到远程服务失败: %s", exc)
            raise

        try:
            json_payload = response.json()
        except ValueError as exc:
            text_body = response.text
            decoder = json.JSONDecoder()
            fragments: list[object] = []
            idx = 0
            length = len(text_body)
            while idx < length:
                while idx < length and text_body[idx].isspace():
                    idx += 1
                if idx >= length:
                    break
                try:
                    obj, offset = decoder.raw_decode(text_body, idx)
                except json.JSONDecodeError:
                    idx += 1
                    continue
                fragments.append(obj)
                idx = offset
            if fragments:
                json_payload = fragments if len(fragments) > 1 else fragments[0]
                logger.warning("远程服务返回非标准 JSON，已拆分解析: %s", exc)
            else:
                logger.error("远程服务返回非 JSON 数据: %s", exc)
                raise

        url, expires_at_raw, remote_mime = self._extract_remote_entry(json_payload)
        if not url:
            logger.error("远程服务返回中缺少 URL 字段: %s", json_payload)
            raise RuntimeError("远程图片服务返回数据无效")

        full_url = self._build_url(url)
        image_id = full_url.rstrip("/").split("/")[-1]

        expires_at = self._parse_expires_at(expires_at_raw)
        if expires_at is None:
            expires_at = time.time() + IMAGE_EXPIRE_SECONDS

        info = RemoteImageInfo(
            id=image_id,
            url=full_url,
            mime_type=remote_mime or mime_type,
            expires_at=expires_at,
        )
        self._images[image_id] = info

        logger.info(
            "图片已上传至远程服务: %s (过期时间: %s)", image_id, expires_at_raw or "N/A"
        )

        return info

    async def get(self, image_id: str) -> Optional[tuple[bytes, str]]:
        """
        获取图片数据
        Returns:
            (图片字节数据, MIME类型) 或 None
        """
        info = self._images.get(image_id)
        fetch_url = (
            info.url
            if info
            else self._build_url(REMOTE_IMAGE_FETCH_PATH.format(id=image_id))
        )

        client = await self._get_client()
        try:
            response = await client.get(fetch_url)
        except httpx.HTTPError as exc:
            logger.error("获取远程图片失败 %s: %s", image_id, exc)
            return None

        if response.status_code != 200:
            logger.warning(
                "远程图片未找到或已过期 %s，状态码: %s", image_id, response.status_code
            )
            if image_id in self._images:
                del self._images[image_id]
            return None

        content_type = response.headers.get("content-type", "image/png")
        if info and content_type != info.mime_type:
            info.mime_type = content_type
        return response.content, content_type

    async def cleanup_expired(self) -> None:
        """清理远程已过期的缓存映射"""
        now = time.time()
        expired = [
            image_id
            for image_id, info in self._images.items()
            if info.expires_at is not None and now > info.expires_at
        ]
        for image_id in expired:
            del self._images[image_id]
        if expired:
            logger.info("清理了 %d 个远程图片缓存映射", len(expired))

    async def start_cleanup_scheduler(self) -> None:
        """启动定期清理任务"""

        async def _scheduler() -> None:
            while True:
                await asyncio.sleep(IMAGE_CLEANUP_INTERVAL)
                try:
                    await self.cleanup_expired()
                except Exception as exc:  # noqa: BLE001
                    logger.error("定期清理失败: %s", exc)

        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(_scheduler())
            logger.info("图片清理调度器已启动，间隔: %s秒", IMAGE_CLEANUP_INTERVAL)

    async def stop_cleanup_scheduler(self) -> None:
        """停止定期清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("图片清理调度器已停止")

        if self._client:
            await self._client.aclose()
            self._client = None


# 全局实例
image_storage = ImageStorage()
