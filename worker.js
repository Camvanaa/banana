// ========== 配置区域 ==========
const API_KEY = "sk-Wghpa37DL7qvlkU2NjR8HoiZXG8qDP72DpjvKdJVRxyyP5KU";
const TARGET_URL = "https://banana.312800.xyz/api/chat";
const PASSWORD = "lehh666";

// 图片传输模式: "kv" 使用KV存储返回URL, "base64" 直接流式传输base64
const IMAGE_MODE = "kv";
const IMAGE_EXPIRE_SECONDS = 3600;

// 支持的模型配置
const ASPECT_RATIOS = ["21-9", "16-9", "4-3", "1-1", "3-4", "9-16", "9-21"];
const IMAGE_SIZES = ["1k", "2k", "4k"];

function generateModelList() {
  const models = [];
  for (const ar of ASPECT_RATIOS) {
    for (const size of IMAGE_SIZES) {
      models.push(`banana-${ar}-${size}`);
    }
  }
  return models;
}

function parseModel(model) {
  const match = model.match(/^banana-(\d+)-(\d+)-(\d+)k$/i);
  if (!match) {
    return { aspectRatio: "21:9", imageSize: "1K" };
  }
  return {
    aspectRatio: `${match[1]}:${match[2]}`,
    imageSize: `${match[3]}K`.toUpperCase(),
  };
}

function validateApiKey(request) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return false;
  const token = authHeader.replace("Bearer ", "");
  return token === API_KEY;
}

function createSSEChunk(data) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

function createDeltaResponse(id, model, delta, finishReason = null) {
  return {
    id: id,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model: model,
    choices: [{ index: 0, delta: delta, finish_reason: finishReason }],
  };
}

// 生成图片ID
function generateImageId() {
  return `img_${Date.now()}_${Math.random().toString(36).substring(2, 10)}`;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const baseUrl = `${url.protocol}//${url.host}`;

    // CORS
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
      });
    }

    // 图片获取端点
    if (url.pathname.startsWith("/v1/images/") && request.method === "GET") {
      const imageId = url.pathname.replace("/v1/images/", "");

      if (!env.IMAGE_KV) {
        return new Response("KV not configured", { status: 500 });
      }

      const imageData = await env.IMAGE_KV.get(imageId, { type: "json" });
      if (!imageData) {
        return new Response("Image not found or expired", { status: 404 });
      }

      const binaryData = Uint8Array.from(atob(imageData.data), (c) =>
        c.charCodeAt(0),
      );
      return new Response(binaryData, {
        headers: {
          "Content-Type": imageData.mimeType,
          "Cache-Control": "public, max-age=3600",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    // 模型列表
    if (url.pathname === "/v1/models" && request.method === "GET") {
      const models = generateModelList().map((id) => ({
        id: id,
        object: "model",
        created: 1700000000,
        owned_by: "banana",
      }));
      return new Response(JSON.stringify({ object: "list", data: models }), {
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Access-Control-Allow-Origin": "*",
        },
      });
    }

    // 聊天补全
    if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
      if (!validateApiKey(request)) {
        return new Response(
          JSON.stringify({
            error: {
              message: "Invalid API key",
              type: "invalid_request_error",
            },
          }),
          {
            status: 401,
            headers: { "Content-Type": "application/json; charset=utf-8" },
          },
        );
      }

      try {
        const body = await request.json();
        const model = body.model || "banana-21-9-1k";
        const messages = body.messages || [];
        const stream = body.stream !== false;

        const lastMessage = messages.filter((m) => m.role === "user").pop();
        let userText = "";
        let userImages = [];

        if (lastMessage) {
          if (typeof lastMessage.content === "string") {
            userText = lastMessage.content;
          } else if (Array.isArray(lastMessage.content)) {
            for (const part of lastMessage.content) {
              if (part.type === "text") {
                userText += part.text;
              } else if (part.type === "image_url") {
                const imgUrl = part.image_url?.url || "";
                if (imgUrl.startsWith("data:")) {
                  userImages.push(imgUrl.split(",")[1]);
                }
              }
            }
          }
        }

        const history = [];
        for (let i = 0; i < messages.length - 1; i++) {
          const msg = messages[i];
          if (msg.role === "user" || msg.role === "assistant") {
            history.push({
              role: msg.role === "user" ? "user" : "model",
              parts: [
                { text: typeof msg.content === "string" ? msg.content : "" },
              ],
            });
          }
        }

        const { aspectRatio, imageSize } = parseModel(model);
        const targetBody = {
          text: userText,
          images: userImages,
          history: history,
          password: PASSWORD,
          temperature: body.temperature || 1,
          topP: body.top_p || 0.95,
          maxTokens: body.max_tokens || 32768,
          aspectRatio: aspectRatio,
          imageSize: imageSize,
          personGeneration: "ALLOW_ALL",
        };

        // 添加 fetch 超时控制 (25秒)
        const controller = new AbortController();
        const fetchTimeout = setTimeout(() => controller.abort(), 25000);

        let response;
        try {
          response = await fetch(TARGET_URL, {
            method: "POST",
            headers: {
              "Content-Type": "application/json; charset=utf-8",
              Origin: "https://banana.312800.xyz",
              Referer: "https://banana.312800.xyz/",
            },
            body: JSON.stringify(targetBody),
            signal: controller.signal,
          });
        } catch (e) {
          clearTimeout(fetchTimeout);
          if (e.name === "AbortError") {
            return new Response(
              JSON.stringify({
                error: {
                  message: "Upstream request timeout",
                  type: "timeout_error",
                },
              }),
              {
                status: 504,
                headers: { "Content-Type": "application/json; charset=utf-8" },
              },
            );
          }
          throw e;
        }
        clearTimeout(fetchTimeout);

        if (!response.ok) {
          return new Response(
            JSON.stringify({
              error: { message: "Upstream API error", type: "api_error" },
            }),
            {
              status: 502,
              headers: { "Content-Type": "application/json; charset=utf-8" },
            },
          );
        }

        const responseId = `chatcmpl-${Date.now()}`;

        if (stream) {
          const { readable, writable } = new TransformStream();
          const writer = writable.getWriter();
          const encoder = new TextEncoder();

          ctx.waitUntil(
            (async () => {
              try {
                await processStreamResponseRealtime(
                  response,
                  writer,
                  encoder,
                  responseId,
                  model,
                  env,
                  baseUrl,
                );
              } catch (e) {
                console.error("Stream error:", e);
              } finally {
                await writer.close();
              }
            })(),
          );

          return new Response(readable, {
            headers: {
              "Content-Type": "text/event-stream; charset=utf-8",
              "Cache-Control": "no-cache",
              Connection: "keep-alive",
              "Access-Control-Allow-Origin": "*",
            },
          });
        } else {
          const text = await response.text();
          const result = await processNonStreamResponse(
            text,
            responseId,
            model,
            env,
            baseUrl,
          );
          return new Response(JSON.stringify(result), {
            headers: {
              "Content-Type": "application/json; charset=utf-8",
              "Access-Control-Allow-Origin": "*",
            },
          });
        }
      } catch (error) {
        return new Response(
          JSON.stringify({
            error: { message: error.message, type: "internal_error" },
          }),
          {
            status: 500,
            headers: { "Content-Type": "application/json; charset=utf-8" },
          },
        );
      }
    }

    return new Response("Not Found", { status: 404 });
  },
};

// 保存图片到 KV 并返回 URL
async function saveImageToKV(env, baseUrl, imageData) {
  if (!env.IMAGE_KV) {
    return null;
  }

  const imageId = generateImageId();
  await env.IMAGE_KV.put(
    imageId,
    JSON.stringify({ mimeType: imageData.mimeType, data: imageData.data }),
    { expirationTtl: IMAGE_EXPIRE_SECONDS },
  );

  return `${baseUrl}/v1/images/${imageId}`;
}

// 解析上游响应数据
function parseUpstreamResponse(text) {
  let jsonData;
  try {
    // 移除 keepalive 注释和前后空白
    let cleanText = text.replace(/\/\*\s*keepalive\s*\*\//g, "").trim();

    // 如果不是以 [ 或 { 开头，尝试找到第一个
    const firstBracket = cleanText.search(/[\[{]/);
    if (firstBracket > 0) {
      cleanText = cleanText.substring(firstBracket);
    }

    jsonData = JSON.parse(cleanText);
  } catch (e) {
    console.error("JSON parse error:", e.message);
    // 尝试分行解析
    const lines = text.split("\n").filter((l) => l.trim());
    for (const line of lines) {
      try {
        const cleanLine = line.replace(/\/\*\s*keepalive\s*\*\//g, "").trim();
        if (cleanLine.startsWith("[") || cleanLine.startsWith("{")) {
          jsonData = JSON.parse(cleanLine);
          break;
        }
      } catch {}
    }
  }

  if (!jsonData) {
    console.error("Failed to parse response, raw text length:", text.length);
    return [];
  }

  return Array.isArray(jsonData) ? jsonData : [jsonData];
}

// 实时流式处理响应
async function processStreamResponseRealtime(
  response,
  writer,
  encoder,
  id,
  model,
  env,
  baseUrl,
) {
  // 发送角色
  await writer.write(
    encoder.encode(
      createSSEChunk(createDeltaResponse(id, model, { role: "assistant" })),
    ),
  );

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let fullText = "";
  let imageData = null;
  let hasReceivedData = false;

  // 带超时的读取函数
  // 首次读取 30 秒，已收到数据后 90 秒（等待图片生成）
  const readWithTimeout = () => {
    const timeout = hasReceivedData ? 90000 : 30000;
    return Promise.race([
      reader.read(),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("read_timeout")), timeout),
      ),
    ]);
  };

  // 逐块读取，带超时保护
  try {
    while (true) {
      const { done, value } = await readWithTimeout();
      if (value) {
        fullText += decoder.decode(value, { stream: true });
        hasReceivedData = true;
      }
      if (done) break;
    }
  } catch (e) {
    if (e.message === "read_timeout") {
      console.error("Read timeout, data length:", fullText.length);
      if (fullText.length === 0) {
        await writer.write(
          encoder.encode(
            createSSEChunk(
              createDeltaResponse(id, model, { content: "[上游响应超时]" }),
            ),
          ),
        );
        await writer.write(
          encoder.encode(
            createSSEChunk(createDeltaResponse(id, model, {}, "stop")),
          ),
        );
        await writer.write(encoder.encode("data: [DONE]\n\n"));
        return;
      }
      // 有部分数据（可能是图片生成超时但有 thinking），继续处理
      console.log("Timeout but has data, processing partial response");
    } else {
      throw e;
    }
  }

  // 解析完整响应
  const parsed = parseUpstreamResponse(fullText);

  if (parsed.length === 0) {
    // 解析失败，输出原始数据长度用于调试
    console.error("Empty parse result, raw length:", fullText.length);
    await writer.write(
      encoder.encode(
        createSSEChunk(
          createDeltaResponse(id, model, {
            content: `[解析失败，原始数据长度: ${fullText.length}]`,
          }),
        ),
      ),
    );
  } else {
    for (const item of parsed) {
      const result = await processChunk(item, writer, encoder, id, model);
      if (result?.imageData) {
        imageData = result.imageData;
      }
    }
  }

  // 发送图片
  if (imageData) {
    let imgContent;
    if (IMAGE_MODE === "kv" && env.IMAGE_KV) {
      const imageUrl = await saveImageToKV(env, baseUrl, imageData);
      if (imageUrl) {
        imgContent = `\n\n![image](${imageUrl})`;
      }
    }
    if (!imgContent) {
      imgContent = `\n\n![image](data:${imageData.mimeType};base64,${imageData.data})`;
    }
    await writer.write(
      encoder.encode(
        createSSEChunk(createDeltaResponse(id, model, { content: imgContent })),
      ),
    );
  }

  // 结束
  await writer.write(
    encoder.encode(createSSEChunk(createDeltaResponse(id, model, {}, "stop"))),
  );
  await writer.write(encoder.encode("data: [DONE]\n\n"));
}

// 处理单个chunk
async function processChunk(item, writer, encoder, id, model) {
  let imageData = null;

  if (!item?.results) return null;

  for (const result of item.results) {
    const candidates = result?.data?.candidates;
    if (!candidates) continue;

    for (const candidate of candidates) {
      const parts = candidate?.content?.parts;
      if (!parts) continue;

      for (const part of parts) {
        if (part.thought === true && part.text) {
          // Thinking - 实时发送
          await writer.write(
            encoder.encode(
              createSSEChunk(
                createDeltaResponse(id, model, {
                  reasoning_content: part.text,
                }),
              ),
            ),
          );
        } else if (part.data === "inlineData" && part.inlineData?.data) {
          // 图片 - 保存稍后发送
          imageData = {
            mimeType: part.inlineData.mimeType || "image/png",
            data: part.inlineData.data,
          };
        } else if (part.data === "text" && part.text && !part.thought) {
          // 文本 - 实时发送
          await writer.write(
            encoder.encode(
              createSSEChunk(
                createDeltaResponse(id, model, { content: part.text }),
              ),
            ),
          );
        }
      }
    }
  }

  return { imageData };
}

// 处理流式响应
async function processStreamResponse(
  text,
  writer,
  encoder,
  id,
  model,
  env,
  baseUrl,
) {
  const jsonData = parseUpstreamResponse(text);

  // 发送角色
  await writer.write(
    encoder.encode(
      createSSEChunk(createDeltaResponse(id, model, { role: "assistant" })),
    ),
  );

  let imageData = null;

  for (const item of jsonData) {
    if (!item?.results) continue;

    for (const result of item.results) {
      const candidates = result?.data?.candidates;
      if (!candidates) continue;

      for (const candidate of candidates) {
        const parts = candidate?.content?.parts;
        if (!parts) continue;

        for (const part of parts) {
          if (part.thought === true && part.text) {
            // Thinking
            await writer.write(
              encoder.encode(
                createSSEChunk(
                  createDeltaResponse(id, model, {
                    reasoning_content: part.text,
                  }),
                ),
              ),
            );
          } else if (part.data === "inlineData" && part.inlineData?.data) {
            // 图片
            imageData = {
              mimeType: part.inlineData.mimeType || "image/png",
              data: part.inlineData.data,
            };
          } else if (part.data === "text" && part.text && !part.thought) {
            // 文本
            await writer.write(
              encoder.encode(
                createSSEChunk(
                  createDeltaResponse(id, model, { content: part.text }),
                ),
              ),
            );
          }
        }
      }
    }
  }

  // 发送图片
  if (imageData) {
    let imgContent;

    if (IMAGE_MODE === "kv" && env.IMAGE_KV) {
      // KV模式：存储并返回URL
      const imageUrl = await saveImageToKV(env, baseUrl, imageData);
      if (imageUrl) {
        imgContent = `\n\n![image](${imageUrl})`;
      }
    }

    if (!imgContent) {
      // base64模式：直接发送data URI
      imgContent = `\n\n![image](data:${imageData.mimeType};base64,${imageData.data})`;
    }

    await writer.write(
      encoder.encode(
        createSSEChunk(createDeltaResponse(id, model, { content: imgContent })),
      ),
    );
  }

  // 结束
  await writer.write(
    encoder.encode(createSSEChunk(createDeltaResponse(id, model, {}, "stop"))),
  );
  await writer.write(encoder.encode("data: [DONE]\n\n"));
}

// 处理非流式响应
async function processNonStreamResponse(text, id, model, env, baseUrl) {
  const jsonData = parseUpstreamResponse(text);

  let thinkingContent = "";
  let textContent = "";
  let imageData = null;

  for (const item of jsonData) {
    if (!item?.results) continue;

    for (const result of item.results) {
      const candidates = result?.data?.candidates;
      if (!candidates) continue;

      for (const candidate of candidates) {
        const parts = candidate?.content?.parts;
        if (!parts) continue;

        for (const part of parts) {
          if (part.thought === true && part.text) {
            thinkingContent += part.text;
          } else if (part.data === "inlineData" && part.inlineData?.data) {
            imageData = {
              mimeType: part.inlineData.mimeType || "image/png",
              data: part.inlineData.data,
            };
          } else if (part.data === "text" && part.text && !part.thought) {
            textContent += part.text;
          }
        }
      }
    }
  }

  // 构建内容
  let finalContent = textContent;
  if (imageData) {
    if (IMAGE_MODE === "kv" && env.IMAGE_KV) {
      const imageUrl = await saveImageToKV(env, baseUrl, imageData);
      if (imageUrl) {
        finalContent += `\n\n![image](${imageUrl})`;
      }
    } else {
      finalContent += `\n\n![image](data:${imageData.mimeType};base64,${imageData.data})`;
    }
  }

  const message = { role: "assistant", content: finalContent };
  if (thinkingContent) {
    message.reasoning_content = thinkingContent;
  }

  return {
    id: id,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: model,
    choices: [{ index: 0, message: message, finish_reason: "stop" }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}
