"""OpenAI 호환 chat/completions 호출과 응답 계약 검사.

계약 통과(HTTP 200, finish_reason=stop, JSON 파싱, 스키마·ID·타입 일치)는 내용 품질과 별개다.
재시도·다수결로 결과를 바꾸지 않는다. 실패는 그대로 기록한다.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from PIL import Image

from .jsonio import sha256_bytes


def png_part(image: Image.Image) -> tuple[dict, dict]:
    """이미지 content part 와 기록용 메타(크기·바이트·해시, base64 제외)."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    blob = buf.getvalue()
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(blob).decode("ascii")}}
    return part, {"size": list(image.size), "bytes": len(blob), "sha256": sha256_bytes(blob),
                  "server_resize_estimate": server_resize(image.size)}


def server_resize(size, budget: int = 2_580_480, multiple: int = 48) -> dict:
    """서버 프로파일 기록의 처리기 리사이즈(√(예산/면적) 배율, 48 배수 내림, 작은 입력 확대) 추정치."""
    w, h = size
    factor = (budget / (w * h)) ** 0.5
    sw, sh = max(multiple, int(w * factor) // multiple * multiple), max(multiple, int(h * factor) // multiple * multiple)
    return {"factor": round(factor, 4), "resized": [sw, sh], "basis": "server-profile-2026-10-24 기록 기반 추정"}


@dataclass
class VlmCall:
    call_id: str
    started_at: str
    seconds: float
    http_status: int | None
    finish_reason: str | None
    usage: dict | None
    content: str | None
    parsed: dict | list | None
    error: str | None
    images: list[dict] = field(default_factory=list)
    request_meta: dict = field(default_factory=dict)
    raw: dict | None = None


class VlmClient:
    def __init__(self, url: str, model: str, timeout_s: float, post=requests.post, prefix: str = "vlm",
                 cache_bust: str | None = None):
        self.url, self.model, self.timeout_s, self._post, self.prefix = url, model, timeout_s, post, prefix
        # 성능 비교용: 요청마다 고유 식별자를 프롬프트 맨 앞에 붙여 서버 prefix cache 재사용을 막는다.
        self.cache_bust = cache_bust
        self.count = 0
        self._lock = threading.Lock()

    def chat(self, text: str, images: list[Image.Image], schema: dict, name: str, max_tokens: int,
             meta: dict | None = None) -> VlmCall:
        with self._lock:   # 동시 호출에서도 call_id 가 겹치지 않게
            self.count += 1
            seq = self.count
        if self.cache_bust:
            text = f"[요청 식별자 {self.cache_bust}-{seq:04d}: 판독과 무관]\n" + text
        parts, image_meta = [{"type": "text", "text": text}], []
        for im in images:
            part, m = png_part(im)
            parts.append(part)
            image_meta.append(m)
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": parts}],
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}}
        call = VlmCall(f"{self.prefix}-{seq:04d}", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), 0.0, None,
                       None, None, None, None, None, image_meta,
                       (meta or {}) | {"prompt": text, "schema": schema, "max_tokens": max_tokens, "temperature": 0})
        started = time.perf_counter()
        try:
            res = self._post(self.url, json=body, timeout=(10, self.timeout_s))
            call.http_status = res.status_code
            if res.status_code != 200:
                call.error = f"HTTP {res.status_code}: {res.text[:500]}"
                return call
            data = res.json()
            call.raw = data
            choice = data["choices"][0]
            call.finish_reason = choice.get("finish_reason")
            call.usage = data.get("usage")
            call.content = choice["message"].get("content")
            if call.finish_reason != "stop":
                call.error = f"finish_reason={call.finish_reason} (출력 잘림 가능)"
            try:
                call.parsed = json.loads(call.content) if call.content is not None else None
            except ValueError as exc:
                call.error = (call.error + "; " if call.error else "") + f"JSON 파싱 실패: {exc}"
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            call.error = f"{type(exc).__name__}: {exc}"
        finally:
            call.seconds = round(time.perf_counter() - started, 3)
        return call
