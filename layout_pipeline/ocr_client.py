"""PaddleX layout-parsing 호출과 호출 기록.

- 요청 기능 옵션을 명시한다. 레이아웃/텍스트 검출 문턱값은 보내지 않으면 서버 기본값이다.
- 보낸 이미지의 크기·바이트·해시와 응답의 width/height 를 대조해 기록한다.
- 실패는 재시도·빈 결과로 숨기지 않고 호출 기록에 오류로 남긴다.
"""
from __future__ import annotations

import base64
import io
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

from .jsonio import dump, sha256_bytes
from .pages import Page
from .routing import InputView

# 기능 옵션만 끈다(방향·왜곡·줄방향 보정, 표 구조·수식·도장 인식). 표 구조 인식을 꺼도 레이아웃의
# table 검출은 수행된다. 검출 문턱값은 여기서 바꾸지 않는다.
BASE_OPTIONS = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": False,
    "useTableRecognition": False,
    "useFormulaRecognition": False,
    "useSealRecognition": False,
    "visualize": False,
}


class OcrError(RuntimeError):
    pass


def text_det_resize_estimate(width: int, height: int, params: dict | None) -> dict | None:
    """서버가 보고한 text_det_params 로 텍스트 검출 입력 배율을 추정한다(서버 코드 미확인 추정치)."""
    if not params:
        return None
    limit, kind, max_side = params.get("limit_side_len"), params.get("limit_type"), params.get("max_side_limit")
    ratio = 1.0
    if kind == "min" and limit and min(width, height) < limit:
        ratio = limit / min(width, height)
    elif kind == "max" and limit and max(width, height) > limit:
        ratio = limit / max(width, height)
    if max_side and max(width, height) * ratio > max_side:
        ratio = max_side / max(width, height)
    return {"ratio": round(ratio, 4), "basis": "estimate from server-reported text_det_params",
            "downscaled": ratio < 1.0}


@dataclass
class CallRecord:
    call_id: str
    page_id: str
    view_id: str
    view_kind: str
    input_crop: list[int]
    sent_size: list[int]
    sent_bytes: int
    sent_sha256: str
    sent_image: str
    options: dict
    started_at: str
    seconds: float
    ok: bool
    http_status: int | None = None
    error: str | None = None
    log_id: str | None = None
    response_size: list[int] | None = None
    response_size_matches: bool | None = None
    model_settings: dict | None = None
    text_det_params: dict | None = None
    text_det_resize: dict | None = None
    raw_path: str | None = None
    counts: dict = field(default_factory=dict)


class OcrClient:
    def __init__(self, url: str, timeout_s: float, options: dict, raw_dir: Path, view_dir: Path,
                 post=requests.post):
        self.url, self.timeout_s, self.options = url, timeout_s, options
        self.raw_dir, self.view_dir, self._post = raw_dir, view_dir, post
        self.calls: list[CallRecord] = []
        self._lock = threading.Lock()
        self._seq = 0

    def _payload_image(self, page: Page, view: InputView) -> tuple[bytes, str, tuple[int, int]]:
        if view.kind == "whole":
            # 페이지 PNG 파일 바이트를 그대로 보낸다(재인코딩 없음).
            blob = Path(page.image_path).read_bytes()
            return blob, page.image_path, (page.width, page.height)
        crop = page.open_image().crop(view.crop)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        blob = buf.getvalue()
        out = self.view_dir / f"{view.view_id}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        return blob, str(out), crop.size

    def call(self, page: Page, view: InputView) -> tuple[CallRecord, dict | None]:
        with self._lock:     # 동시 호출에서도 call_id 가 겹치지 않게
            self._seq += 1
            call_id = f"ocr-{self._seq:04d}"
        blob, sent_path, size = self._payload_image(page, view)
        with Image.open(io.BytesIO(blob)) as check:
            if check.size != size:
                raise OcrError(f"{view.view_id}: 보낼 이미지 크기 불일치 {check.size} != {size}")
        record = CallRecord(call_id, page.page_id, view.view_id, view.kind, list(view.crop), list(size), len(blob),
                            sha256_bytes(blob), sent_path, dict(self.options),
                            datetime.now(timezone.utc).isoformat(timespec="milliseconds"), 0.0, False)
        body = {"fileType": 1, "file": base64.b64encode(blob).decode("ascii"), **self.options}
        started = time.perf_counter()
        pruned = None
        try:
            response = self._post(self.url, json=body, timeout=(10, self.timeout_s))
            record.http_status = response.status_code
            response.raise_for_status()
            data = response.json()
            record.log_id = data.get("logId")
            if data.get("errorCode") not in (0, None):
                raise OcrError(f"errorCode={data.get('errorCode')} {data.get('errorMsg')}")
            results = data["result"]["layoutParsingResults"]
            if len(results) != 1:
                raise OcrError(f"layoutParsingResults 길이 {len(results)} != 1")
            pruned = results[0]["prunedResult"]
            raw_path = self.raw_dir / f"{call_id}.json"
            dump(raw_path, data)
            record.raw_path = str(raw_path)
            record.response_size = [pruned.get("width"), pruned.get("height")]
            record.response_size_matches = record.response_size == list(size)
            record.model_settings = pruned.get("model_settings")
            record.text_det_params = pruned.get("overall_ocr_res", {}).get("text_det_params")
            record.text_det_resize = text_det_resize_estimate(size[0], size[1], record.text_det_params)
            record.counts = {"layout_det_boxes": len(pruned["layout_det_res"]["boxes"]),
                             "ocr_items": len(pruned["overall_ocr_res"]["rec_texts"]),
                             "parsing_res_blocks": len(pruned.get("parsing_res_list") or [])}
            if not record.response_size_matches:
                raise OcrError(f"응답 크기 {record.response_size} != 보낸 크기 {list(size)}")
            record.ok = True
        except (requests.RequestException, OcrError, KeyError, ValueError) as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            pruned = None
        finally:
            record.seconds = round(time.perf_counter() - started, 3)
            with self._lock:
                self.calls.append(record)
        return record, pruned
