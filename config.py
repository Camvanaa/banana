"""
配置管理模块
"""

import os
from pathlib import Path

# ========== API 配置 ==========
API_KEY = os.getenv("API_KEY", "sk-Wghpa37DL7qvlkU2NjR8HoiZXG8qDP72DpjvKdJVRxyyP5KU")
TARGET_URL = os.getenv("TARGET_URL", "https://banana.312800.xyz/api/chat")
PASSWORD = os.getenv("PASSWORD", "lehh666")

# ========== 服务器配置 ==========
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "6667"))
BASE_URL = os.getenv(
    "BASE_URL", "http://140.245.33.121:6667"
)  # 公网访问 URL，如 https://your-domain.com

# ========== 图片存储配置 ==========
IMAGE_DIR = Path(os.getenv("IMAGE_DIR", "./images"))
IMAGE_EXPIRE_SECONDS = int(os.getenv("IMAGE_EXPIRE_SECONDS", "3600"))  # 1小时过期
IMAGE_CLEANUP_INTERVAL = int(
    os.getenv("IMAGE_CLEANUP_INTERVAL", "300")
)  # 5分钟清理一次

# ========== 模型配置 ==========
ASPECT_RATIOS = ["auto", "21-9", "16-9", "4-3", "1-1", "3-4", "9-16", "9-21"]
IMAGE_SIZES = ["1k", "2k", "4k"]

# 确保图片目录存在
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
