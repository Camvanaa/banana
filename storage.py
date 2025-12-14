"""Remote image storage helper with upload retries and background cleanup."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence
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


@dataclass(slots=True)
class RemoteImageInfo:
    """Metadata for an image stored on the remote service."""

    id: str
    url: str
    mime_type: str
    expires_at: Optional[float]


class ImageStorage:
    """Manage remote image uploads and cached references."""

    def __init__(self) -> None:
        self._images: dict[str, RemoteImageInfo] = {}
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @staticmethod
    def _build_url(path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        base = REMOTE_IMAGE_BASE_URL.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))

    @staticmethod
    def _parse_expires_at(raw: Optional[str]) -> Optional[float]:
        if not raw:
            return None
        try:
            normalized = raw.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            logger.debug("Unable to parse expires_at value: %s", raw)
            return None

    @staticmethod
    def _candidate_dicts(payload: object) -> Iterable[dict]:
        if isinstance(payload, dict):
            yield payload
            for key in ("data", "result", "results", "images", "items"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    yield from ImageStorage._candidate_dicts(nested)
                elif isinstance(nested, list):
                    for item in nested:
                        yield from ImageStorage._candidate_dicts(item)
        elif isinstance(payload, list):
            for item in payload:
                yield from ImageStorage._candidate_dicts(item)

    @staticmethod
    def _extract_remote_entry(
        payload: object,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        for candidate in ImageStorage._candidate_dicts(payload):
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
                fallback = REMOTE_IMAGE_FETCH_PATH.format(id=identifier)
                return fallback, expires_at, mime_type

        return None, None, None

    @staticmethod
    def _build_upload_payloads(data: str, mime_type: str) -> Sequence[dict]:
        metadata = {
            "success": True,
            "image_base64": data,
            "mime_type": mime_type,
            "is_valid_base64": True,
            "extraction_note": "proxy",
        }

        try:
            decoded_len = len(base64.b64decode(data, validate=True))

        except Exception:  # noqa: BLE001
            decoded_len = None

        if decoded_len is not None:
            metadata["data_length"] = decoded_len

        return [metadata]

    @staticmethod
    def _parse_json_stream(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            text = response.text
            decoder = json.JSONDecoder()
            fragments: list[object] = []
            index = 0
            length = len(text)

            while index < length:
                while index < length and text[index].isspace():
                    index += 1
                if index >= length:
                    break
                try:
                    obj, offset = decoder.raw_decode(text, index)
                except json.JSONDecodeError:
                    index += 1
                    continue
                fragments.append(obj)
                index = offset

            if fragments:
                logger.warning(
                    "Non-standard JSON response parsed into %d fragments: %s",
                    len(fragments),
                    exc,
                )
                return fragments if len(fragments) > 1 else fragments[0]

            logger.error("Unable to parse JSON response: %s", exc)
            raise

    async def save(self, data: str, mime_type: str = "image/png") -> RemoteImageInfo:
        """Upload base64-encoded image to the remote service."""
        upload_url = self._build_url(REMOTE_IMAGE_UPLOAD_PATH)
        payload_variants = self._build_upload_payloads(data, mime_type)
        client = await self._get_client()

        response: Optional[httpx.Response] = None
        last_exception: Optional[Exception] = None
        last_error: Optional[str] = None

        for payload in payload_variants:
            try:
                candidate = await client.post(upload_url, json=payload)
            except httpx.HTTPError as exc:
                last_exception = exc
                last_error = str(exc)
                logger.error(
                    "Upload attempt failed with exception (payload keys=%s): %s",
                    list(payload.keys()),
                    exc,
                )
                continue

            if candidate.status_code >= 400:
                preview = candidate.text[:500]
                last_error = f"{candidate.status_code}: {preview}"
                logger.warning(
                    "Upload attempt rejected (payload keys=%s): %s",
                    list(payload.keys()),
                    preview,
                )
                continue

            response = candidate
            break

        if response is None:
            if last_exception is not None:
                raise last_exception
            raise RuntimeError(f"Upload failed: {last_error or 'unknown error'}")

        payload = self._parse_json_stream(response)
        url, expires_at_raw, remote_mime = self._extract_remote_entry(payload)
        if not url:
            logger.error("Remote response missing URL field: %s", payload)
            raise RuntimeError("Remote image service returned invalid data")

        resolved_url = self._build_url(url)
        image_id = resolved_url.rstrip("/").split("/")[-1]
        expires_at = self._parse_expires_at(expires_at_raw) or (
            time.time() + IMAGE_EXPIRE_SECONDS
        )

        info = RemoteImageInfo(
            id=image_id,
            url=resolved_url,
            mime_type=remote_mime or mime_type,
            expires_at=expires_at,
        )
        self._images[image_id] = info

        logger.info(
            "Image uploaded to remote storage: %s (expires: %s)",
            image_id,
            expires_at_raw or "N/A",
        )
        return info

    async def get(self, image_id: str) -> Optional[tuple[bytes, str]]:
        """Retrieve image bytes from remote service."""
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
            logger.error("Failed to fetch remote image %s: %s", image_id, exc)
            return None

        if response.status_code != 200:
            logger.warning(
                "Remote image unavailable %s (status %s)",
                image_id,
                response.status_code,
            )
            self._images.pop(image_id, None)
            return None

        content_type = response.headers.get("content-type", "image/png")
        if info and content_type != info.mime_type:
            info.mime_type = content_type

        return response.content, content_type

    async def cleanup_expired(self) -> None:
        """Remove cached mappings that have passed their expiration."""
        now = time.time()
        expired = [
            image_id
            for image_id, info in self._images.items()
            if info.expires_at is not None and now > info.expires_at
        ]

        for image_id in expired:
            del self._images[image_id]

        if expired:
            logger.info("Cleaned up %d expired remote image references", len(expired))

    async def start_cleanup_scheduler(self) -> None:
        """Start periodic cleanup task."""

        async def _scheduler() -> None:
            while True:
                await asyncio.sleep(IMAGE_CLEANUP_INTERVAL)
                try:
                    await self.cleanup_expired()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Scheduled cleanup failed: %s", exc)

        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(_scheduler())
            logger.info(
                "Remote image cleanup scheduler started (interval=%ss)",
                IMAGE_CLEANUP_INTERVAL,
            )

    async def stop_cleanup_scheduler(self) -> None:
        """Stop periodic cleanup task and close HTTP client."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("Remote image cleanup scheduler stopped")

        if self._client:
            await self._client.aclose()
            self._client = None


# Shared instance
image_storage = ImageStorage()
