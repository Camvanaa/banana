"""
Banana API 代理服务器 - OpenAI 兼容格式
支持并发、流式响应、图片存储与定期清理
"""

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from config import (
    API_KEY,
    ASPECT_RATIOS,
    BASE_URL,
    HOST,
    IMAGE_SIZES,
    PASSWORD,
    PORT,
    TARGET_URL,
)
from storage import image_storage

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ========== 数据模型 ==========


class Message(BaseModel):
    role: str
    content: str | list


class ChatCompletionRequest(BaseModel):
    model: str = "banana-21-9-1k"
    messages: list[Message]
    stream: bool = True
    temperature: float = 1.0
    top_p: float = 0.95
    max_tokens: int = 32768


# ========== 生命周期管理 ==========


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    await image_storage.start_cleanup_scheduler()
    logger.info("服务器启动完成")

    yield

    # 关闭时
    await image_storage.stop_cleanup_scheduler()
    logger.info("服务器已关闭")


app = FastAPI(
    title="Banana API Proxy",
    description="OpenAI 兼容的图片生成 API 代理",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 工具函数 ==========


def generate_model_list() -> list[str]:
    """生成支持的模型列表"""
    models = []
    for ar in ASPECT_RATIOS:
        for size in IMAGE_SIZES:
            models.append(f"banana-{ar}-{size}")
    return models


def parse_model(model: str) -> tuple[str, str]:
    """解析模型名称获取宽高比和尺寸"""
    # 支持 auto 模式: banana-auto-1k
    auto_match = re.match(r"^banana-auto-(\d+)k$", model, re.IGNORECASE)
    if auto_match:
        return "auto", f"{auto_match.group(1)}K".upper()

    # 常规模式: banana-21-9-1k
    match = re.match(r"^banana-(\d+)-(\d+)-(\d+)k$", model, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:{match.group(2)}", f"{match.group(3)}K".upper()

    # 默认值
    return "auto", "1K"


def get_base_url(request: Request) -> str:
    """获取服务的基础 URL"""

    if BASE_URL:
        return BASE_URL.rstrip("/")

    headers = request.headers

    def _first(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        return value.split(",")[0].strip() or None

    forwarded_proto = _first(headers.get("x-forwarded-proto"))

    forwarded_host = _first(headers.get("x-forwarded-host"))

    forwarded_port = _first(headers.get("x-forwarded-port"))

    forwarded = headers.get("forwarded")

    if forwarded:
        first_forwarded = forwarded.split(",", 1)[0]

        for part in first_forwarded.split(";"):
            key, _, value = part.strip().partition("=")

            if not value:
                continue

            key = key.lower()
            value = value.strip().strip('"')

            if key == "proto" and not forwarded_proto:
                forwarded_proto = _first(value)

            elif key == "host" and not forwarded_host:
                forwarded_host = _first(value)

    proto = (forwarded_proto or request.url.scheme).strip()

    host = forwarded_host or _first(headers.get("host"))

    if host:
        default_ports = {"http": "80", "https": "443"}

        if forwarded_port and ":" not in host:
            proto_key = proto.lower()

            if default_ports.get(proto_key) != forwarded_port:
                host = f"{host}:{forwarded_port}"

        return f"{proto}://{host}".rstrip("/")

    return f"{proto}://{request.url.netloc}".rstrip("/")


def validate_api_key(request: Request) -> bool:
    """验证 API Key"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    return token == API_KEY


async def require_api_key(request: Request):
    """API Key 依赖检查"""
    if not validate_api_key(request):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {"message": "Invalid API key", "type": "invalid_request_error"}
            },
        )


# ========== SSE 工具函数 ==========


def create_sse_chunk(data: dict) -> str:
    """创建 SSE 数据块"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_delta_response(
    response_id: str, model: str, delta: dict, finish_reason: Optional[str] = None
) -> dict:
    """创建流式响应的 delta 格式"""
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


# ========== 上游响应解析 ==========


def parse_upstream_response(text: str) -> list:
    """解析上游响应数据"""
    try:
        # 移除 keepalive 注释和前后空白
        clean_text = re.sub(r"/\*\s*keepalive\s*\*/", "", text).strip()

        # 如果不是以 [ 或 { 开头，尝试找到第一个
        first_bracket = -1
        for i, c in enumerate(clean_text):
            if c in "[{":
                first_bracket = i
                break

        if first_bracket > 0:
            clean_text = clean_text[first_bracket:]

        json_data = json.loads(clean_text)
        return json_data if isinstance(json_data, list) else [json_data]

    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析错误: {e}")
        # 尝试分行解析
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            clean_line = re.sub(r"/\*\s*keepalive\s*\*/", "", line).strip()
            if clean_line.startswith("[") or clean_line.startswith("{"):
                try:
                    json_data = json.loads(clean_line)
                    return json_data if isinstance(json_data, list) else [json_data]
                except json.JSONDecodeError:
                    continue

    logger.error(f"解析失败，原始数据长度: {len(text)}")
    return []


def extract_content_from_parsed(parsed: list) -> tuple[str, str, Optional[dict]]:
    """
    从解析后的数据中提取内容

    Returns:
        (thinking_content, text_content, image_data)
    """
    thinking_content = ""
    text_content = ""
    image_data = None

    for item in parsed:
        results = item.get("results") if isinstance(item, dict) else None
        if not results:
            continue

        for result in results:
            data = result.get("data") if isinstance(result, dict) else None
            if not data:
                continue

            candidates = data.get("candidates", [])
            for candidate in candidates:
                content = candidate.get("content", {})
                parts = content.get("parts", [])

                for part in parts:
                    if part.get("thought") is True and part.get("text"):
                        thinking_content += part["text"]
                    elif part.get("data") == "inlineData" and part.get(
                        "inlineData", {}
                    ).get("data"):
                        inline = part["inlineData"]
                        image_data = {
                            "mime_type": inline.get("mimeType", "image/png"),
                            "data": inline["data"],
                        }
                    elif (
                        part.get("data") == "text"
                        and part.get("text")
                        and not part.get("thought")
                    ):
                        text_content += part["text"]

    return thinking_content, text_content, image_data


# ========== API 端点 ==========


@app.get("/v1/models")
async def list_models():
    """获取模型列表"""
    models = [
        {"id": model_id, "object": "model", "created": 1700000000, "owned_by": "banana"}
        for model_id in generate_model_list()
    ]
    return {"object": "list", "data": models}


@app.get("/v1/images/{image_id}")
async def get_image(image_id: str):
    """获取存储的图片"""
    result = await image_storage.get(image_id)
    if not result:
        raise HTTPException(status_code=404, detail="Image not found or expired")

    image_bytes, mime_type = result
    return Response(
        content=image_bytes,
        media_type=mime_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """聊天补全接口"""

    # 提取用户消息
    user_text = ""
    user_images = []

    user_messages = [m for m in body.messages if m.role == "user"]
    if user_messages:
        last_msg = user_messages[-1]
        if isinstance(last_msg.content, str):
            user_text = last_msg.content
        elif isinstance(last_msg.content, list):
            for part in last_msg.content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        user_text += part.get("text", "")
                    elif part.get("type") == "image_url":
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:"):
                            # 提取 base64 数据
                            parts = img_url.split(",", 1)
                            if len(parts) == 2:
                                user_images.append(parts[1])

    # 构建历史记录
    history = []
    for msg in body.messages[:-1]:
        if msg.role in ("user", "assistant"):
            content = msg.content if isinstance(msg.content, str) else ""
            history.append(
                {
                    "role": "user" if msg.role == "user" else "model",
                    "parts": [{"text": content}],
                }
            )

    # 解析模型参数
    aspect_ratio, image_size = parse_model(body.model)

    # 构建上游请求
    target_body = {
        "text": user_text,
        "images": user_images,
        "history": history,
        "password": PASSWORD,
        "temperature": body.temperature,
        "topP": body.top_p,
        "maxTokens": body.max_tokens,
        "aspectRatio": aspect_ratio,
        "imageSize": image_size,
        "personGeneration": "ALLOW_ALL",
    }

    response_id = f"chatcmpl-{int(time.time() * 1000)}"

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                TARGET_URL,
                json=target_body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Origin": "https://banana.312800.xyz",
                    "Referer": "https://banana.312800.xyz/",
                },
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {"message": "Upstream API error", "type": "api_error"}
                    },
                )

            response_text = response.text

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail={
                "error": {
                    "message": "Upstream request timeout",
                    "type": "timeout_error",
                }
            },
        )
    except httpx.RequestError as e:
        logger.error(f"请求上游失败: {e}")
        raise HTTPException(
            status_code=502, detail={"error": {"message": str(e), "type": "api_error"}}
        )

    # 解析响应
    parsed = parse_upstream_response(response_text)
    thinking_content, text_content, image_data = extract_content_from_parsed(parsed)

    # 处理图片
    image_url = None
    if image_data:
        try:
            remote_info = await image_storage.save(
                image_data["data"], image_data["mime_type"]
            )
            image_url = remote_info.url
        except Exception as e:
            logger.error(f"保存图片失败: {e}")

    if body.stream:
        # 流式响应
        return StreamingResponse(
            stream_response(
                response_id, body.model, thinking_content, text_content, image_url
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
    else:
        # 非流式响应
        final_content = text_content
        if image_url:
            final_content += f"\n\n![image]({image_url})"

        message = {"role": "assistant", "content": final_content}
        if thinking_content:
            message["reasoning_content"] = thinking_content

        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


async def stream_response(
    response_id: str,
    model: str,
    thinking_content: str,
    text_content: str,
    image_url: Optional[str],
) -> AsyncGenerator[str, None]:
    """生成流式响应"""
    # 发送角色
    yield create_sse_chunk(
        create_delta_response(response_id, model, {"role": "assistant"})
    )

    # 发送 thinking 内容（分块）
    if thinking_content:
        chunk_size = 100
        for i in range(0, len(thinking_content), chunk_size):
            chunk = thinking_content[i : i + chunk_size]
            yield create_sse_chunk(
                create_delta_response(response_id, model, {"reasoning_content": chunk})
            )
            await asyncio.sleep(0.01)  # 模拟流式效果

    # 发送文本内容（分块）
    if text_content:
        chunk_size = 50
        for i in range(0, len(text_content), chunk_size):
            chunk = text_content[i : i + chunk_size]
            yield create_sse_chunk(
                create_delta_response(response_id, model, {"content": chunk})
            )
            await asyncio.sleep(0.01)

    # 发送图片
    if image_url:
        img_content = f"\n\n![image]({image_url})"
        yield create_sse_chunk(
            create_delta_response(response_id, model, {"content": img_content})
        )

    # 发送结束
    yield create_sse_chunk(create_delta_response(response_id, model, {}, "stop"))
    yield "data: [DONE]\n\n"


# ========== 健康检查 ==========


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "timestamp": int(time.time())}


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Banana API Proxy",
        "version": "1.0.0",
        "endpoints": {
            "models": "/v1/models",
            "chat": "/v1/chat/completions",
            "images": "/v1/images/{image_id}",
        },
    }


# ========== 主程序入口 ==========


if __name__ == "__main__":
    import uvicorn

    logger.info(f"启动服务器: {HOST}:{PORT}")
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        workers=4,  # 多进程支持并发
        log_level="info",
    )
