import json
import logging
import os
import time
import httpx
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import AzureOpenAI
import redis

class Prompt(BaseModel):
    messages: Optional[List[Dict[str, str]]] = None
    message: Optional[str] = None
    chat_id: Optional[str] = None

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview")
APIM_BASE_URL = os.getenv("APIM_BASE_URL")
APIM_SUBSCRIPTION_KEY = os.getenv("APIM_SUBSCRIPTION_KEY")
APIM_API_SUFFIX = os.getenv("APIM_API_SUFFIX", "").strip("/")

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_SSL = os.getenv("REDIS_SSL", "false").lower() == "true"
REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))

if not AZURE_OPENAI_DEPLOYMENT:
    raise RuntimeError("AZURE_OPENAI_DEPLOYMENT must be set")

if not APIM_BASE_URL:
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set when APIM_BASE_URL is not provided")

# Azure OpenAI client (API key)
client = None
if not APIM_BASE_URL:
    client = AzureOpenAI(
        api_key=AZURE_OPENAI_API_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
    )

redis_client = None
if REDIS_HOST:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        ssl=REDIS_SSL,
        decode_responses=True,
    )

def _load_history(chat_id: str) -> List[Dict[str, str]]:
    if not redis_client:
        return []
    data = redis_client.get(f"chat:{chat_id}")
    return json.loads(data) if data else []

def _chat_title(messages: List[Dict[str, str]]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()[:25] or "New Chat"
    return "New Chat"

def _save_history(chat_id: str, messages: List[Dict[str, str]]) -> None:
    if not redis_client:
        return
    now = int(time.time())
    redis_client.setex(f"chat:{chat_id}", REDIS_TTL, json.dumps(messages))
    meta = {"title": _chat_title(messages), "updated_at": now}
    redis_client.setex(f"chatmeta:{chat_id}", REDIS_TTL, json.dumps(meta))
    redis_client.zadd("chat:updated", {chat_id: now})
    redis_client.expire("chat:updated", REDIS_TTL)

def _list_chats() -> List[Dict[str, str]]:
    if not redis_client:
        return []
    ids = redis_client.zrevrange("chat:updated", 0, -1)
    chats = []
    for chat_id in ids:
        meta_raw = redis_client.get(f"chatmeta:{chat_id}")
        if not meta_raw:
            continue
        meta = json.loads(meta_raw)
        chats.append(
            {
                "id": chat_id,
                "title": meta.get("title", "New Chat"),
                "updated_at": meta.get("updated_at", 0),
            }
        )
    return chats

def _delete_chat(chat_id: str) -> None:
    if not redis_client:
        return
    redis_client.delete(f"chat:{chat_id}", f"chatmeta:{chat_id}")
    redis_client.zrem("chat:updated", chat_id)

def _clear_chats() -> None:
    if not redis_client:
        return
    ids = redis_client.zrange("chat:updated", 0, -1)
    if ids:
        keys = [f"chat:{cid}" for cid in ids] + [f"chatmeta:{cid}" for cid in ids]
        redis_client.delete(*keys)
    redis_client.delete("chat:updated")

def _record_stats(backend_id: str, usage: Dict[str, int], latency_ms: int, cache_hit: Optional[bool]) -> None:
    if not redis_client:
        return
    key = f"stats:backend:{backend_id}"
    redis_client.hincrby(key, "responses", 1)
    redis_client.hincrby(key, "prompt_tokens", int(usage.get("prompt_tokens", 0)))
    redis_client.hincrby(key, "completion_tokens", int(usage.get("completion_tokens", 0)))
    redis_client.hincrby(key, "total_tokens", int(usage.get("total_tokens", 0)))
    redis_client.hincrby(key, "latency_ms_total", int(latency_ms))
    redis_client.hset(key, "backend_id", backend_id)
    if cache_hit is True:
        redis_client.hincrby(key, "cache_hits", 1)
    elif cache_hit is False:
        redis_client.hincrby(key, "cache_misses", 1)

def _get_stats() -> List[Dict[str, int]]:
    if not redis_client:
        return []
    stats = []
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match="stats:backend:*", count=100)
        for key in keys:
            data = redis_client.hgetall(key)
            if not data:
                continue
            stats.append(
                {
                    "backend_id": data.get("backend_id", key.replace("stats:backend:", "")),
                    "responses": int(data.get("responses", 0)),
                    "prompt_tokens": int(data.get("prompt_tokens", 0)),
                    "completion_tokens": int(data.get("completion_tokens", 0)),
                    "total_tokens": int(data.get("total_tokens", 0)),
                    "latency_ms_total": int(data.get("latency_ms_total", 0)),
                    "cache_hits": int(data.get("cache_hits", 0)),
                    "cache_misses": int(data.get("cache_misses", 0)),
                }
            )
        if cursor == 0:
            break
    return stats

def _clear_stats() -> None:
    if not redis_client:
        return
    cursor = 0
    keys_to_delete = []
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match="stats:backend:*", count=100)
        keys_to_delete.extend(keys)
        if cursor == 0:
            break
    if keys_to_delete:
        redis_client.delete(*keys_to_delete)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/chats")
async def list_chats():
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    return _list_chats()

@app.get("/chats/{chat_id}")
async def get_chat(chat_id: str):
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    return _load_history(chat_id)

@app.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str):
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    _delete_chat(chat_id)
    return {"status": "ok"}

@app.delete("/chats")
async def clear_chats():
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    _clear_chats()
    return {"status": "ok"}

@app.get("/stats")
async def stats():
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    return _get_stats()

@app.delete("/stats")
async def clear_stats():
    if not redis_client:
        raise HTTPException(status_code=400, detail="Redis is not configured")
    _clear_stats()
    return {"status": "ok"}

@app.post("/chat")
async def chat(p: Prompt):
    try:
        messages: List[Dict[str, str]] = []

        if redis_client and p.chat_id:
            messages = _load_history(p.chat_id)
            if p.message:
                messages.append({"role": "user", "content": p.message})
            elif p.messages:
                messages = p.messages
        elif p.messages:
            messages = p.messages
        elif p.message:
            messages = [{"role": "user", "content": p.message}]

        if not messages:
            raise RuntimeError("messages or message must be provided")

        start = time.time()
        backend_id = None
        cache_hit = None
        if APIM_BASE_URL:
            base = APIM_BASE_URL.rstrip("/")
            prefix = f"/{APIM_API_SUFFIX}" if APIM_API_SUFFIX else ""
            url = f"{base}{prefix}/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions"
            headers = {"Content-Type": "application/json"}
            if APIM_SUBSCRIPTION_KEY:
                headers["Ocp-Apim-Subscription-Key"] = APIM_SUBSCRIPTION_KEY
            params = {"api-version": AZURE_OPENAI_API_VERSION}
            payload = {"messages": messages}
            with httpx.Client(timeout=60.0) as http:
                r = http.post(url, headers=headers, params=params, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"APIM error {r.status_code}: {r.text}")
            backend_id = r.headers.get("x-openai-backend")
            cache_header = r.headers.get("x-semantic-cache")
            if cache_header is not None:
                cache_hit = cache_header.strip().upper() == "HIT"
            response_json = r.json()
            response_text = response_json["choices"][0]["message"]["content"]
            usage = response_json.get("usage", {})
        else:
            response = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=messages
            )
            response_text = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        latency_ms = int((time.time() - start) * 1000)

        if redis_client and p.chat_id:
            messages.append(
                {
                    "role": "assistant",
                    "content": response_text,
                    "meta": {
                        "latency_ms": latency_ms,
                        "usage": usage,
                        "backend_id": backend_id,
                        "cache_hit": cache_hit,
                    },
                }
            )
            _save_history(p.chat_id, messages)
            _record_stats(backend_id or "direct", usage, latency_ms, cache_hit)

        return {
            "text": response_text,
            "latency_ms": latency_ms,
            "usage": usage,
            "backend_id": backend_id,
            "cache_hit": cache_hit,
        }

    except Exception as e:
        logger.exception("Chat request failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
