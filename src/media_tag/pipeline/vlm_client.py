"""Consumer stage 1: one async request pool shared by images and videos.

Concurrency is expressed as weighted slots on a single semaphore (image=1,
video=2) instead of two rigid pools. A request that cannot be admitted waits on
the condition; it never blocks the event loop. HTTP uses a shared
``httpx.AsyncClient`` with an explicit connection pool, timeout and retry policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

import httpx

from .config import InferenceConfig
from .metrics import Metrics
from .models import ProcessedItem, TaggedItem

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = "pipeline-tag-v1"
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

SYSTEM_PROMPT = (
    "你是素材打标器。只依据随消息提供的画面证据判断，不得使用文件名、路径或任何外部知识。"
    "证据不足、无法判断或画面不适用时，该字段必须输出 null，不得猜测。"
    "只输出一个 JSON 对象，不要输出 Markdown、解释或额外文本。"
    "JSON 必须恰好包含以下字段，不多不少：{fields}。"
    "字段值为字符串或 null；不要编造疾病、许可、人物身份或拍摄地点。"
)


class WeightedLimiter:
    """Admission control for a shared slot budget (images 1, video montages 2)."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._used = 0
        self._condition = asyncio.Condition()
        self._waiting = 0

    async def acquire(self, weight: int = 1) -> None:
        if weight > self.capacity:
            raise ValueError(f"request weight {weight} exceeds slot capacity {self.capacity}")
        async with self._condition:
            self._waiting += 1
            try:
                while self._used + weight > self.capacity:
                    await self._condition.wait()
                self._used += weight
            finally:
                self._waiting -= 1

    async def release(self, weight: int = 1) -> None:
        async with self._condition:
            self._used = max(0, self._used - weight)
            self._condition.notify_all()

    @property
    def in_flight(self) -> int:
        return self._used


def load_field_schema(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("properties"), dict):
        raise ValueError(f"invalid tag field schema: {path}")
    required = payload.get("required")
    if not isinstance(required, list) or not required:
        raise ValueError(f"tag field schema must declare required fields: {path}")
    if set(required) != set(payload["properties"]):
        raise ValueError("tag field schema required/properties mismatch")
    return payload


def field_names(schema: dict[str, Any]) -> tuple[str, ...]:
    return tuple(schema["required"])


def _type_ok(value: Any, spec: dict[str, Any]) -> bool:
    expected = spec.get("type")
    if expected is None:
        return True
    types = (expected,) if isinstance(expected, str) else tuple(expected)
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, (int, float)):
        return "number" in types or "integer" in types
    if isinstance(value, str):
        return "string" in types
    if isinstance(value, list):
        if "array" not in types:
            return False
        items = spec.get("items")
        return all(_type_ok(item, items) for item in value) if isinstance(items, dict) else True
    if isinstance(value, dict):
        return "object" in types
    return False


def validate_fields(payload: Any, schema: dict[str, Any], *, strict_keys: bool) -> str | None:
    """Return None when valid, otherwise a short diagnostic string."""

    if not isinstance(payload, dict):
        return "response is not a JSON object"
    required = set(schema["required"])
    present = set(payload)
    missing = sorted(required - present)
    if missing:
        return f"missing fields: {missing}"
    extra = sorted(present - required)
    if extra and strict_keys:
        return f"unexpected fields: {extra}"
    for name in sorted(required):
        if not _type_ok(payload[name], schema["properties"][name]):
            return f"field type mismatch: {name}"
    return None


class VlmClient:
    """Tag one :class:`ProcessedItem` with retries and local schema validation."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: InferenceConfig,
        schema: dict[str, Any],
        metrics: Metrics,
        *,
        limiter: WeightedLimiter | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._schema = schema
        self._metrics = metrics
        self._fields = field_names(schema)
        self._limiter = limiter or WeightedLimiter(config.slots)
        self._endpoint = config.base_url.rstrip("/") + "/chat/completions"
        api_key = os.environ.get(config.api_key_env)
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._system_prompt = SYSTEM_PROMPT.format(fields="、".join(self._fields))

    # -- request construction -------------------------------------------------
    def _weight(self, item: ProcessedItem) -> int:
        return self._config.video_slots if item.media.kind == "video" else self._config.image_slots

    def _user_content(self, item: ProcessedItem, attempt: int) -> list[dict[str, Any]]:
        kind = item.media.kind
        if kind == "video":
            media = item.meta.get("montage", {})
            note = (
                "这是一条视频的等间隔采样拼图："
                f"{media.get('columns')} 列 × {media.get('rows')} 行，按行从左到右、从上到下排列，"
                f"共 {media.get('cells')} 格；空格为无效画面。请把整张拼图作为一个连续片段整体判断。"
            )
        else:
            note = "这是一张图片，已按长边缩放并做 EXIF 方向纠正。"
        if attempt > 0:
            note += "上一次回复不是合法 JSON，请只输出符合字段契约的 JSON 对象。"
        content: list[dict[str, Any]] = [{"type": "text", "text": note}]
        for encoded in item.images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + encoded},
                }
            )
        content.append(
            {
                "type": "text",
                "text": "请输出 JSON，字段为：" + "、".join(self._fields) + "。",
            }
        )
        return content

    def _payload(self, item: ProcessedItem, attempt: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": self._user_content(item, attempt)},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        if self._config.structured_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "media_tag_fields",
                    "strict": True,
                    "schema": self._schema,
                },
            }
        elif self._config.structured_mode == "guided_json":
            # vLLM reads guided decoding options as top-level request fields.
            payload["guided_json"] = self._schema
        return payload

    # -- response handling ----------------------------------------------------
    @staticmethod
    def _parse(content: str) -> Any:
        text = _CODE_FENCE.sub("", content.strip())
        return json.loads(text)

    async def tag(self, item: ProcessedItem) -> TaggedItem | None:
        """Return a validated tag result, or None after exhausting retries."""

        weight = self._weight(item)
        last_error = "unknown"
        last_raw = ""
        for attempt in range(self._config.max_retries):
            await self._limiter.acquire(weight)
            started = asyncio.get_running_loop().time()
            try:
                response = await self._client.post(
                    self._endpoint,
                    json=self._payload(item, attempt),
                    headers=self._headers,
                )
                last_raw = response.text
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                payload = self._parse(content)
                error = validate_fields(payload, self._schema, strict_keys=self._config.strict_keys)
                if error is not None:
                    raise ValueError(error)
                latency = asyncio.get_running_loop().time() - started
                usage = body.get("usage") or {}
                self._metrics.observe_latency(latency)
                self._metrics.observe_tokens(
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                )
                self._metrics.incr("inferred")
                return TaggedItem(
                    processed=item,
                    fields={name: payload[name] for name in self._fields},
                    raw_response=last_raw,
                    model=str(body.get("model") or self._config.model),
                    latency_seconds=latency,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                )
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            finally:
                await self._limiter.release(weight)
            if attempt + 1 < self._config.max_retries:
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                await asyncio.sleep(delay)
        self._metrics.incr("infer_failed")
        LOGGER.warning("tagging failed: %s", last_error, extra={"sha256": item.media.sha256})
        # The caller writes this to failed_records.jsonl; keep the raw reply bounded.
        item.meta["_last_error"] = last_error
        item.meta["_last_raw"] = last_raw[:4000]
        return None


def failure_record(item: ProcessedItem, *, error: str, raw: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sha256": item.media.sha256,
        "path": str(item.media.path),
        "kind": item.media.kind,
        "error": error,
        "raw_response": raw[:4000],
    }


def encode_media_preview(item: ProcessedItem) -> dict[str, Any]:
    """Small metadata block for audit logs (never the base64 payload)."""

    return {
        "sha256": item.media.sha256,
        "path": str(item.media.path),
        "kind": item.media.kind,
        "wire_bytes": item.wire_bytes,
        "preprocess_seconds": round(item.preprocess_seconds, 3),
        "meta": {key: value for key, value in item.meta.items() if not key.startswith("_")},
    }


__all__ = [
    "VlmClient",
    "WeightedLimiter",
    "field_names",
    "load_field_schema",
    "validate_fields",
    "failure_record",
    "encode_media_preview",
]
