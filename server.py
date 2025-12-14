"""
Banana API proxy server with streaming heartbeats and enhanced upstream handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from config import (
    API_KEY,
    ASPECT_RATIOS,
    HOST,
    IMAGE_SIZES,
    PASSWORD,
    PORT,
    TARGET_URL,
)
from storage import image_storage

logger = logging.getLogger(__name__)


HTTP_TIMEOUT = httpx.Timeout(600.0)


HEARTBEAT_INTERVAL_SECONDS = 10
THINKING_CHUNK_SIZE = 100
CONTENT_CHUNK_SIZE = 50


# =========================
# Models
# =========================


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


@dataclass
class UpstreamResult:
    thinking: str
    text: str
    image: Optional[dict]


# =========================
# FastAPI App
# =========================


@asynccontextmanager
async def lifespan(app: FastAPI):
    await image_storage.start_cleanup_scheduler()
    logger.info("服务器启动完成")
    try:
        yield
    finally:
        await image_storage.stop_cleanup_scheduler()
        logger.info("服务器已关闭")


app = FastAPI(
    title="Banana API Proxy",
    description="OpenAI 兼容的图片生成 API 代理",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# Helpers
# =========================


def generate_model_list() -> list[str]:
    models = []
    for ar in ASPECT_RATIOS:
        for size in IMAGE_SIZES:
            models.append(f"banana-{ar}-{size}")
    return models


def parse_model(model: str) -> tuple[str, str]:
    auto_match = re.match(r"^banana-auto-(\d+)k$", model, re.IGNORECASE)
    if auto_match:
        return "auto", f"{auto_match.group(1)}K".upper()

    match = re.match(r"^banana-(\d+)-(\d+)-(\d+)k$", model, re.IGNORECASE)
    if match:
        return f"{match.group(1)}:{match.group(2)}", f"{match.group(3)}K".upper()

    return "auto", "1K"


def validate_api_key(request: Request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    return token == API_KEY


async def require_api_key(request: Request):
    if not validate_api_key(request):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid API key",
                    "type": "invalid_request_error",
                }
            },
        )


def create_sse_chunk(data: dict | str) -> str:
    if isinstance(data, str):
        return f"{data}\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_delta_response(
    response_id: str, model: str, delta: dict, finish_reason: Optional[str] = None
) -> dict:
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


async def persist_image(image_payload: Optional[dict]) -> Optional[str]:
    if not image_payload or not image_payload.get("data"):
        return None
    try:
        remote = await image_storage.save(
            image_payload["data"], image_payload.get("mime_type", "image/png")
        )
        return remote.url
    except Exception as exc:  # noqa: BLE001
        logger.error("保存图片失败: %s", exc)
        return None


def parse_upstream_text(text: str) -> UpstreamResult:
    # Remove SSE format if present (data: {...})
    clean_text = text.strip()

    # If it's SSE format, extract the JSON payload
    if clean_text.startswith("data:"):
        lines = []
        for line in clean_text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                json_part = line[5:].strip()
                if json_part and json_part != "[DONE]":
                    lines.append(json_part)

        if lines:
            # Try to parse as JSON array or single object
            if len(lines) == 1:
                clean_text = lines[0]
            else:
                clean_text = "[" + ",".join(lines) + "]"

    # Remove keepalive comments
    clean_text = re.sub(r"/\*\s*keepalive\s*\*/", "", clean_text).strip()

    try:
        first_bracket = next((i for i, c in enumerate(clean_text) if c in "[{]"), -1)
        if first_bracket > 0:
            clean_text = clean_text[first_bracket:]
        payload = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        logger.error("JSON 解析错误: %s", exc)
        logger.error("Failed text preview: %s", text[:500])
        payload = []

    if isinstance(payload, dict):
        payload = [payload]

    thinking = ""
    content = ""
    image_data = None

    for item in payload:
        results = item.get("results") if isinstance(item, dict) else None
        if not results:
            continue

        for result in results:
            data = result.get("data") if isinstance(result, dict) else None
            if not data:
                continue

            for candidate in data.get("candidates", []):
                parts = candidate.get("content", {}).get("parts", [])
                for part in parts:
                    if part.get("thought") and part.get("text"):
                        thinking += part["text"]
                    elif part.get("data") == "inlineData" and part.get(
                        "inlineData", {}
                    ).get("data"):
                        image_data = {
                            "data": part["inlineData"]["data"],
                            "mime_type": part["inlineData"].get(
                                "mimeType", "image/png"
                            ),
                        }
                    elif (
                        part.get("data") == "text"
                        and part.get("text")
                        and not part.get("thought")
                    ):
                        content += part["text"]

    return UpstreamResult(thinking=thinking, text=content, image=image_data)


async def fetch_upstream(payload: dict) -> UpstreamResult:
    logger.info("Fetching upstream with payload keys: %s", list(payload.keys()))
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                TARGET_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Origin": "https://banana.312800.xyz",
                    "Referer": "https://banana.312800.xyz/",
                },
            )
            response.raise_for_status()
            logger.info(
                "Upstream response status: %s, length: %d",
                response.status_code,
                len(response.text),
            )
            logger.debug("Upstream response preview: %s", response.text[:500])
            result = parse_upstream_text(response.text)
            logger.info(
                "Parsed result - thinking: %d chars, text: %d chars, has_image: %s",
                len(result.thinking),
                len(result.text),
                bool(result.image),
            )
            return result
    except httpx.TimeoutException as exc:
        logger.warning("Upstream request timeout: %s", exc)
        raise HTTPException(
            status_code=504,
            detail={
                "error": {
                    "message": "Upstream request timeout",
                    "type": "timeout_error",
                }
            },
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Upstream API error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "Upstream API error", "type": "api_error"}},
        ) from exc
    except httpx.RequestError as exc:
        logger.error("请求上游失败: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": str(exc), "type": "api_error"}},
        ) from exc


def chunk_text(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def stream_chat_response(
    response_id: str,
    model: str,
    upstream_future: "asyncio.Task[UpstreamResult]",
) -> AsyncGenerator[str, None]:
    result: Optional[UpstreamResult] = None
    logger.info("Starting stream_chat_response for id=%s, model=%s", response_id, model)
    try:
        # Send initial chunk to establish connection
        yield create_sse_chunk(
            create_delta_response(response_id, model, {"role": "assistant"})
        )
        logger.debug("Sent initial role chunk")

        while not upstream_future.done():
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(upstream_future), timeout=HEARTBEAT_INTERVAL_SECONDS
                )
                break
            except asyncio.TimeoutError:
                # Send empty chunk as keepalive
                yield create_sse_chunk(create_delta_response(response_id, model, {}))
            except (HTTPException, Exception) as exc:
                message = "Upstream request failed"
                if isinstance(exc, HTTPException):
                    detail = getattr(exc, "detail", None)
                    if isinstance(detail, dict):
                        message = detail.get("error", {}).get("message", message)
                    elif detail:
                        message = str(detail)
                else:
                    message = str(exc) or message

                logger.error("Stream error during upstream fetch: %s", message)
                yield create_sse_chunk(
                    create_delta_response(
                        response_id,
                        model,
                        {"content": f"[Error] {message}"},
                        finish_reason="error",
                    )
                )
                yield "data: [DONE]\n\n"
                return

        if result is None:
            logger.warning("Stream completed with empty result")
            yield create_sse_chunk(
                create_delta_response(
                    response_id,
                    model,
                    {"content": "[Error] Empty upstream result"},
                    finish_reason="error",
                )
            )
            yield "data: [DONE]\n\n"
            return

        logger.info(
            "Processing result: thinking=%d, text=%d, has_image=%s",
            len(result.thinking),
            len(result.text),
            bool(result.image),
        )
        image_url = await persist_image(result.image)

        if result.thinking:
            for chunk in chunk_text(result.thinking, THINKING_CHUNK_SIZE):
                yield create_sse_chunk(
                    create_delta_response(
                        response_id, model, {"reasoning_content": chunk}
                    )
                )

        if result.text:
            for chunk in chunk_text(result.text, CONTENT_CHUNK_SIZE):
                yield create_sse_chunk(
                    create_delta_response(response_id, model, {"content": chunk})
                )

        if image_url:
            image_block = f"\n\n![image]({image_url})"
            yield create_sse_chunk(
                create_delta_response(response_id, model, {"content": image_block})
            )

        yield create_sse_chunk(
            create_delta_response(response_id, model, {}, finish_reason="stop")
        )
        yield "data: [DONE]\n\n"

    except GeneratorExit:
        logger.info("Client disconnected during streaming")
        raise
    except Exception as exc:
        logger.error("Unexpected streaming error: %s", exc, exc_info=True)
        try:
            yield create_sse_chunk(
                create_delta_response(
                    response_id,
                    model,
                    {"content": f"[Error] {str(exc) or 'Streaming failed'}"},
                    finish_reason="error",
                )
            )
            yield "data: [DONE]\n\n"
        except:
            pass
    finally:
        if not upstream_future.done():
            upstream_future.cancel()


# =========================
# Endpoints
# =========================


@app.get("/v1/models")
async def list_models():
    models = [
        {"id": model_id, "object": "model", "created": 1700000000, "owned_by": "banana"}
        for model_id in generate_model_list()
    ]
    return {"object": "list", "data": models}


@app.get("/v1/images/{image_id}")
async def get_image(image_id: str):
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
    # Extract user text and images
    user_text = ""
    user_images: list[str] = []

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
                            split = img_url.split(",", 1)
                            if len(split) == 2:
                                user_images.append(split[1])

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

    aspect_ratio, image_size = parse_model(body.model)

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
    logger.info(
        "Creating upstream request for response_id: %s, model: %s",
        response_id,
        body.model,
    )
    logger.debug(
        "Request payload: user_text=%s, images=%d",
        user_text[:100] if user_text else "(empty)",
        len(user_images),
    )
    upstream_future: "asyncio.Task[UpstreamResult]" = asyncio.create_task(
        fetch_upstream(target_body)
    )

    if body.stream:

        async def stream_wrapper():
            logger.info("Stream wrapper started for response_id: %s", response_id)
            try:
                async for chunk in stream_chat_response(
                    response_id, body.model, upstream_future
                ):
                    yield chunk
            except Exception as exc:
                logger.error("Stream wrapper caught error: %s", exc, exc_info=True)
                raise
            finally:
                logger.info(
                    "Stream wrapper cleanup, upstream_done=%s", upstream_future.done()
                )
                if not upstream_future.done():
                    upstream_future.cancel()

        response = StreamingResponse(
            stream_wrapper(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )

        async def cleanup_task():
            try:
                await upstream_future
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("Upstream task error: %s", exc)

        if not hasattr(request.app.state, "background_tasks"):
            request.app.state.background_tasks = set()
        request.app.state.background_tasks.add(asyncio.create_task(cleanup_task()))

        return response

    result = await upstream_future

    image_url = await persist_image(result.image)
    final_content = result.text
    if image_url:
        final_content += f"\n\n![image]({image_url})"

    message = {"role": "assistant", "content": final_content}
    if result.thinking:
        message["reasoning_content"] = result.thinking

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": int(time.time())}


@app.get("/")
async def root():
    return {
        "service": "Banana API Proxy",
        "version": "1.1.0",
        "endpoints": {
            "models": "/v1/models",
            "chat": "/v1/chat/completions",
            "images": "/v1/images/{image_id}",
        },
    }


if __name__ == "__main__":
    import uvicorn

    logger.info("启动服务器: %s:%s", HOST, PORT)
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        workers=4,
        log_level="info",
    )
