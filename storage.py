"""
图片存储和清理服务
"""

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import IMAGE_CLEANUP_INTERVAL, IMAGE_DIR, IMAGE_EXPIRE_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class ImageInfo:
    """图片信息"""

    id: str
    path: Path
    mime_type: str
    created_at: float


class ImageStorage:
    """图片存储管理器"""

    def __init__(self):
        self._images: dict[str, ImageInfo] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    def generate_id(self) -> str:
        """生成唯一图片ID"""
        return f"img_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    async def save(self, data: str, mime_type: str = "image/png") -> str:
        """
        保存 base64 图片数据到本地

        Args:
            data: base64 编码的图片数据
            mime_type: MIME 类型

        Returns:
            图片ID
        """
        image_id = self.generate_id()

        # 确定文件扩展名
        ext = "png"
        if "jpeg" in mime_type or "jpg" in mime_type:
            ext = "jpg"
        elif "gif" in mime_type:
            ext = "gif"
        elif "webp" in mime_type:
            ext = "webp"

        file_path = IMAGE_DIR / f"{image_id}.{ext}"

        # 解码并保存
        try:
            image_bytes = base64.b64decode(data)
            file_path.write_bytes(image_bytes)

            self._images[image_id] = ImageInfo(
                id=image_id, path=file_path, mime_type=mime_type, created_at=time.time()
            )

            logger.info(f"图片已保存: {image_id}, 大小: {len(image_bytes)} 字节")
            return image_id

        except Exception as e:
            logger.error(f"保存图片失败: {e}")
            raise

    def get(self, image_id: str) -> Optional[tuple[bytes, str]]:
        """
        获取图片数据

        Returns:
            (图片字节数据, MIME类型) 或 None
        """
        info = self._images.get(image_id)

        if not info:
            # 尝试从文件系统查找
            logger.info(f"内存中未找到 {image_id}，尝试从文件系统查找: {IMAGE_DIR}")
            found_files = list(IMAGE_DIR.glob(f"{image_id}.*"))
            logger.info(f"找到文件: {found_files}")

            for file_path in found_files:
                if file_path.exists():
                    mime_type = self._guess_mime_type(file_path.suffix)
                    logger.info(f"从文件系统加载图片: {file_path}")
                    return file_path.read_bytes(), mime_type

            logger.warning(f"图片未找到: {image_id}")
            return None

        if not info.path.exists():
            del self._images[image_id]
            return None

        return info.path.read_bytes(), info.mime_type

    def _guess_mime_type(self, suffix: str) -> str:
        """根据扩展名猜测MIME类型"""
        mapping = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        return mapping.get(suffix.lower(), "image/png")

    async def cleanup_expired(self):
        """清理过期图片"""
        now = time.time()
        expired_ids = []

        for image_id, info in list(self._images.items()):
            if now - info.created_at > IMAGE_EXPIRE_SECONDS:
                expired_ids.append(image_id)
                try:
                    if info.path.exists():
                        info.path.unlink()
                        logger.info(f"已删除过期图片: {image_id}")
                except Exception as e:
                    logger.error(f"删除图片失败 {image_id}: {e}")

        for image_id in expired_ids:
            del self._images[image_id]

        # 同时清理文件系统中的孤立文件
        await self._cleanup_orphan_files()

        if expired_ids:
            logger.info(f"清理了 {len(expired_ids)} 个过期图片")

    async def _cleanup_orphan_files(self):
        """清理文件系统中的孤立文件（超过过期时间的）"""
        now = time.time()
        for file_path in IMAGE_DIR.glob("img_*.*"):
            try:
                # 从文件名解析时间戳
                parts = file_path.stem.split("_")
                if len(parts) >= 2:
                    created_ts = int(parts[1])
                    if now - created_ts > IMAGE_EXPIRE_SECONDS:
                        file_path.unlink()
                        logger.info(f"已删除孤立文件: {file_path.name}")
            except (ValueError, IndexError):
                continue
            except Exception as e:
                logger.error(f"清理孤立文件失败 {file_path}: {e}")

    async def start_cleanup_scheduler(self):
        """启动定期清理任务"""

        async def _scheduler():
            while True:
                await asyncio.sleep(IMAGE_CLEANUP_INTERVAL)
                try:
                    await self.cleanup_expired()
                except Exception as e:
                    logger.error(f"定期清理失败: {e}")

        self._cleanup_task = asyncio.create_task(_scheduler())
        logger.info(f"图片清理调度器已启动，间隔: {IMAGE_CLEANUP_INTERVAL}秒")

    async def stop_cleanup_scheduler(self):
        """停止定期清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("图片清理调度器已停止")


# 全局实例
image_storage = ImageStorage()
