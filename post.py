#!/usr/bin/env python3
"""
Banana Worker 测试脚本
用于测试 OpenAI 兼容 API 的各项功能
"""

import base64
import json
import re
from pathlib import Path

import requests

# ========== 配置区域 ==========
WORKER_URL = "https://banana.349988099.workers.dev"
API_KEY = "sk-Wghpa37DL7qvlkU2NjR8HoiZXG8qDP72DpjvKdJVRxyyP5KU"

HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}


def test_models():
    """测试获取模型列表"""
    print("=" * 50)
    print("测试: GET /v1/models")
    print("=" * 50)

    resp = requests.get(f"{WORKER_URL}/v1/models", headers=HEADERS)
    print(f"状态码: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"可用模型数量: {len(data.get('data', []))}")
        print("模型列表:")
        for model in data.get("data", [])[:5]:
            print(f"  - {model['id']}")
        if len(data.get("data", [])) > 5:
            print(f"  ... 还有 {len(data['data']) - 5} 个模型")
    else:
        print(f"错误: {resp.text}")
    print()


def test_chat_stream(prompt="你好", model="banana-21-9-1k", debug=False):
    """测试流式聊天"""
    print("=" * 50)
    print(f"测试: POST /v1/chat/completions (流式, model={model})")
    print(f"Prompt: {prompt}")
    print("=" * 50)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 1,
        "top_p": 0.95,
        "max_tokens": 32768,
    }

    resp = requests.post(
        f"{WORKER_URL}/v1/chat/completions", headers=HEADERS, json=body, stream=True
    )

    print(f"状态码: {resp.status_code}")
    print("-" * 50)

    if debug:
        print("[DEBUG] 原始响应:")
        raw = resp.text
        print(raw[:500] if len(raw) > 500 else raw)
        print("-" * 50)
        return

    thinking_content = ""
    text_content = ""
    image_data = None
    image_url = None
    chunk_count = 0

    for line in resp.iter_lines():
        if not line:
            continue

        line = line.decode("utf-8")
        chunk_count += 1

        if line.startswith("data: "):
            data_str = line[6:]
            if data_str == "[DONE]":
                print("\n[DONE]")
                break

            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})

                # 处理 reasoning_content (thinking)
                if "reasoning_content" in delta:
                    thinking = delta["reasoning_content"]
                    thinking_content += thinking
                    print(
                        f"[Thinking] {thinking[:50]}..."
                        if len(thinking) > 50
                        else f"[Thinking] {thinking}"
                    )

                # 处理普通文本
                if "content" in delta:
                    content = delta["content"]
                    if isinstance(content, str):
                        text_content += content
                        # 检查是否包含图片URL
                        url = extract_image_url(content)
                        if url:
                            image_url = url
                            print(f"\n[Image URL] {url}")
                        else:
                            print(f"[Text] {content}", end="", flush=True)
                    elif isinstance(content, list):
                        for item in content:
                            if item.get("type") == "image_url":
                                img_url = item.get("image_url", {}).get("url", "")
                                if img_url.startswith("data:"):
                                    parts = img_url.split(",", 1)
                                    if len(parts) == 2:
                                        image_data = parts[1]
                                        print(
                                            f"\n[Image] 收到图片数据, 长度: {len(image_data)} 字符"
                                        )

                # 处理 finish_reason
                finish = data.get("choices", [{}])[0].get("finish_reason")
                if finish:
                    print(f"\n[Finish] {finish}")

            except json.JSONDecodeError as e:
                print(f"[Error] JSON解析错误: {e}")

    print("-" * 50)
    print(f"收到 {chunk_count} 个chunks")
    print(f"Thinking 总长度: {len(thinking_content)} 字符")
    print(f"Text 总长度: {len(text_content)} 字符")
    print(f"图片: {'有' if (image_data or image_url) else '无'}")

    # 保存图片
    if image_data:
        save_image(image_data, "output_stream.png")
    elif image_url:
        download_image(image_url, "output_stream.png")

    print()
    return {
        "thinking": thinking_content,
        "text": text_content,
        "image": image_data or image_url,
    }


def test_chat_non_stream(prompt="你好", model="banana-21-9-1k"):
    """测试非流式聊天"""
    print("=" * 50)
    print(f"测试: POST /v1/chat/completions (非流式, model={model})")
    print(f"Prompt: {prompt}")
    print("=" * 50)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 1,
        "top_p": 0.95,
        "max_tokens": 32768,
    }

    resp = requests.post(
        f"{WORKER_URL}/v1/chat/completions", headers=HEADERS, json=body
    )

    print(f"状态码: {resp.status_code}")
    print("-" * 50)

    if resp.status_code == 200:
        data = resp.json()
        print(f"响应 ID: {data.get('id')}")
        print(f"Model: {data.get('model')}")

        message = data.get("choices", [{}])[0].get("message", {})

        # Thinking
        if "reasoning_content" in message:
            thinking = message["reasoning_content"]
            print(f"\n[Thinking] ({len(thinking)} 字符)")
            print(thinking[:200] + "..." if len(thinking) > 200 else thinking)

        # Content
        content = message.get("content", "")
        if isinstance(content, str):
            print(f"\n[Text] {content}")
        elif isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    print(f"\n[Text] {item.get('text', '')}")
                elif item.get("type") == "image_url":
                    img_url = item.get("image_url", {}).get("url", "")
                    if img_url.startswith("data:"):
                        parts = img_url.split(",", 1)
                        if len(parts) == 2:
                            print(f"\n[Image] 收到图片, 长度: {len(parts[1])} 字符")
                            save_image(parts[1], "output_non_stream.png")

        print(f"\nFinish Reason: {data.get('choices', [{}])[0].get('finish_reason')}")
    else:
        print(f"错误: {resp.text}")

    print()


def test_image_generation(prompt="画一只可爱的猪"):
    """测试图片生成"""
    print("=" * 50)
    print(f"测试: 图片生成")
    print(f"Prompt: {prompt}")
    print("=" * 50)

    return test_chat_stream(prompt, model="banana-21-9-1k")


def save_image(base64_data, filename):
    """保存 base64 图片到文件"""
    try:
        img_bytes = base64.b64decode(base64_data)
        path = Path(filename)
        path.write_bytes(img_bytes)
        print(f"[保存] 图片已保存到: {path.absolute()}")
    except Exception as e:
        print(f"[错误] 保存图片失败: {e}")


def download_image(url, filename):
    """从 URL 下载图片"""
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            path = Path(filename)
            path.write_bytes(resp.content)
            print(f"[保存] 图片已下载到: {path.absolute()}")
        else:
            print(f"[错误] 下载图片失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"[错误] 下载图片失败: {e}")


def extract_image_url(text):
    """从 markdown 文本中提取图片 URL"""
    match = re.search(r"!\[.*?\]\((https?://[^)]+)\)", text)
    return match.group(1) if match else None


def test_invalid_key():
    """测试无效 API Key"""
    print("=" * 50)
    print("测试: 无效 API Key")
    print("=" * 50)

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer invalid-key",
    }

    body = {
        "model": "banana-21-9-1k",
        "messages": [{"role": "user", "content": "test"}],
        "stream": False,
    }

    resp = requests.post(
        f"{WORKER_URL}/v1/chat/completions", headers=headers, json=body
    )

    print(f"状态码: {resp.status_code}")
    print(f"响应: {resp.text}")
    print(f"预期: 401 Unauthorized")
    print()


def main():
    print("\n" + "=" * 60)
    print("Banana Worker 测试工具")
    print(f"Worker URL: {WORKER_URL}")
    print("=" * 60 + "\n")

    # 1. 测试模型列表
    test_models()

    # 2. 测试无效 Key
    test_invalid_key()

    # 快速调试模式
    print("是否启用调试模式查看原始响应? (y/n): ", end="")
    if input().strip().lower() == "y":
        test_chat_stream("你好", "banana-1-1-1k", debug=True)
        return

    # 3. 测试图片生成
    print("是否测试图片生成? (y/n): ", end="")
    if input().strip().lower() == "y":
        test_image_generation("画一只可爱的小猪在农场里")


if __name__ == "__main__":
    main()
