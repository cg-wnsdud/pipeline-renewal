"""예금보험공사 예금자보호 표시(보호·비보호 로고) 확인(deposit-marks/1, docs/21).

기존 parser-pipeline(nh-ad-compliance dev 186ddca, nh_parser_fin/parse/deposit_marks.py)의 방식과 프롬프트를 그대로 옮겼다.

- 후보: 영역 최종 문구나 그 영역에 배정된 OCR 글자(stage1 기록, 있으면)에 로고 글자(예금보험공사·보호금융상품·비보호)가 있는 영역.
  같은 글자는 예금자보호 안내 문장·'보호금융상품등록부' 메뉴에도 나오므로 글자만으로는 표시라고 하지 않는다. 메뉴 이름은 빼고,
  글자가 짧은(로고에 가까운) 영역부터 MAX_CANDIDATES 개만 확인한다.
- 확인: 후보 영역 crop(여백은 흰색으로 가림)을 VLM 에 보여 보호·비보호 표시마다 YES/NO/UNSURE 를 받는다.
- 결과: 표시마다 {status: PRESENT | ABSENT | UNKNOWN, code(UNKNOWN 사유), region_ids(표시가 보인 영역)}. 표시 안 글자(mark_text)와
  후보별 응답은 trace 에만 둔다. 이 상품에 표시가 있어야 하는지는 심의가 정한다. 처리하지 못한 쪽이 있으면 ABSENT 대신
  UNKNOWN(PAGES_INCOMPLETE)이다.
"""
from __future__ import annotations

import os
import re

from PIL import Image

VERSION = "deposit-marks/1"
KINDS = ("protected", "non_protected")
TRIGGERS = ("예금보험공사", "보호금융상품", "비보호")
# 웹 화면 하단 메뉴(보안센터 | … | 보호금융상품등록부)는 쪽마다 반복된다. 메뉴 이름은 표시가 아니므로 후보 글자에서 뺀다.
MENU_NAMES = re.compile(r"보호금융상품(?:등록부|목록|조회)")
MAX_CANDIDATES = 12
ANSWERS = ("YES", "NO", "UNSURE")
CROP_PAD = 12
MIN_CROP_SIDE = 320
MAX_SIDE = 1400
READ_PAGE_STATUSES = ("READY", "CANDIDATE_RUN")

PROMPT = """이미지는 금융상품 광고의 한 부분을 잘라낸 것입니다.
예금보험공사의 예금자보호 표시(로고)가 이 이미지에 있는지 보호 표시와 비보호 표시를 따로 판단하세요.

- 보호 표시(protected): 테두리가 있는 작은 사각 그림 안에 '예금보험공사'(작은 글씨), '보호금융상품'(큰 글씨),
  '1인당 최고 ○억원'(작은 글씨)이 적힌 표시입니다. 금액은 광고마다 다를 수 있습니다.
- 비보호 표시(non_protected): 테두리가 있는 작은 사각 그림 안에 '예금보험공사'(작은 글씨)와 '비보호'(큰 빨간
  글씨)가 적힌 표시입니다. 빗금이 그어져 있기도 합니다.

답하는 기준:
- YES: 그 표시 그림이 이미지 어딘가에 보입니다. 표시가 작거나 해상도가 낮아도 테두리와 글자 구성으로
  알아볼 수 있으면 YES입니다. 표·카드·문단 옆에 붙어 있어도 됩니다.
- NO: 그 표시 그림이 없습니다. 글자로 쓴 문장만 있는 경우(예: '이 예금은 예금자보호법에 따라 … 보호됩니다',
  '이 상품은 예금자보호법에 따라 보호되지 않습니다'), 메뉴·링크 이름(보호금융상품등록부)만 있는 경우는 NO입니다.
  한 종류의 표시만 보이면 다른 종류는 NO입니다.
- UNSURE: 표시처럼 보이는 그림이 있지만 잘리거나 뭉개져서 어느 표시인지 구별할 수 없을 때만 씁니다.

mark_text에는 보이는 표시 안의 글자를 줄바꿈 없이 적고, 표시가 없으면 빈 문자열로 두세요."""

SCHEMA = {
    "type": "object",
    "properties": {
        "protected": {"type": "string", "enum": list(ANSWERS)},
        "non_protected": {"type": "string", "enum": list(ANSWERS)},
        "mark_text": {"type": "string"},
    },
    "required": ["protected", "non_protected", "mark_text"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return os.environ.get("LP_DEPOSIT_MARKS", "on").strip().lower() != "off"


def _compact(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def candidates(b: dict, ocr_texts: dict[str, list[str]]) -> list[dict]:
    """로고 글자가 있는 영역(묶음 별칭 b 의 영역). 글자가 짧은 영역부터, 같으면 페이지 순."""
    order = {pa: i for i, pa in enumerate(b["page_order"])}
    found = []
    for a, r in b["regions"].items():
        texts = [MENU_NAMES.sub("", _compact(t)) for t in [r["text"], *ocr_texts.get(r["region_id"], [])] if t]
        if not r["bbox"] or not any(trigger in t for t in texts for trigger in TRIGGERS):
            continue
        page = b["pages"][r["page"]]
        found.append({"alias": a, "region_id": r["region_id"], "page_id": page["page_id"], "bbox": list(r["bbox"]),
                      "text_length": len(_compact(r["text"])), "page_order": order[r["page"]]})
    return sorted(found, key=lambda c: (c["text_length"], c["page_order"]))


def crop(image: Image.Image, bbox) -> Image.Image | None:
    """영역 crop. 여백은 글자가 잘리지 않게 두되 bbox 밖은 흰색으로 가린다(이웃 영역을 읽지 않게). 작은 crop 은 키운다."""
    x0, y0 = max(0, int(bbox[0]) - CROP_PAD), max(0, int(bbox[1]) - CROP_PAD)
    x1, y1 = min(image.width, int(bbox[2]) + CROP_PAD), min(image.height, int(bbox[3]) + CROP_PAD)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    raw = image.crop((x0, y0, x1, y1)).convert("RGB")
    out = Image.new("RGB", raw.size, "white")
    inner = (max(0, int(bbox[0]) - x0), max(0, int(bbox[1]) - y0), min(raw.width, int(bbox[2]) - x0), min(raw.height, int(bbox[3]) - y0))
    if inner[2] <= inner[0] or inner[3] <= inner[1]:
        return None
    out.paste(raw.crop(inner), (inner[0], inner[1]))
    scale = MIN_CROP_SIDE / max(1, min(out.size))
    if scale > 1.0:
        scale = min(scale, 4.0)
        out = out.resize((max(1, round(out.width * scale)), max(1, round(out.height * scale))), Image.LANCZOS)
    out.thumbnail((MAX_SIDE, MAX_SIDE))
    return out


def request(b: dict, cand: dict, images: dict[str, Image.Image]) -> dict | None:
    """후보 하나의 확인 요청. crop 을 만들 수 없으면 None."""
    page = b["pages"][b["regions"][cand["alias"]]["page"]]
    if page["image"] not in images:
        with Image.open(page["image"]) as handle:
            images[page["image"]] = handle.convert("RGB")
    im = crop(images[page["image"]], cand["bbox"])
    if im is None:
        return None
    return {"version": VERSION, "text": PROMPT, "images": [im], "schema": SCHEMA, "max_tokens": 300}


def check(call) -> dict:
    """응답 → {protected, non_protected: YES|NO|UNSURE, mark_text} 또는 {error}."""
    if call is None:
        return {"error": "빈 crop"}
    if call.error or not isinstance(call.parsed, dict):
        return {"error": (call.error or "응답 없음")[:200]}
    p = call.parsed
    out = {k: (str(p.get(k) or "UNSURE").upper()) for k in KINDS}
    return {k: v if v in ANSWERS else "UNSURE" for k, v in out.items()} | {"mark_text": str(p.get("mark_text") or "").strip()[:120]}


def summarize(checks: list[dict], unchecked: int, complete: bool) -> dict:
    """표시마다 상태. checks: [{region_id, page_id, bbox, protected, non_protected, mark_text} | {…, error}]."""
    out = {"version": VERSION}
    for kind in KINDS:
        seen = [c["region_id"] for c in checks if c.get(kind) == "YES"]
        if seen:
            out[kind] = {"status": "PRESENT", "code": None, "region_ids": seen}
        elif any("error" in c for c in checks):
            out[kind] = {"status": "UNKNOWN", "code": "CHECK_FAILED", "region_ids": []}
        elif any(c.get(kind) == "UNSURE" for c in checks):
            out[kind] = {"status": "UNKNOWN", "code": "MARK_UNCLEAR", "region_ids": []}
        elif unchecked:
            out[kind] = {"status": "UNKNOWN", "code": "TOO_MANY_CANDIDATES", "region_ids": []}
        elif not complete:
            out[kind] = {"status": "UNKNOWN", "code": "PAGES_INCOMPLETE", "region_ids": []}
        else:
            out[kind] = {"status": "ABSENT", "code": None, "region_ids": []}
    return out


def not_checked(code: str) -> dict:
    """확인하지 않음(DISABLED: 끔, NOT_CHECKED: 이 기능 이전 실행의 재구성)."""
    return {"version": VERSION, **{k: {"status": "UNKNOWN", "code": code, "region_ids": []} for k in KINDS}}


def pages_complete(b: dict) -> bool:
    """모든 쪽이 처리됐고 판독에 실패한 영역이 없음(없다고 말할 수 있는 조건)."""
    return all(p["status"] in READ_PAGE_STATUSES and not any(b["regions"][a]["text_status"] == "read_failed" for a in p["regions"])
               for p in b["pages"].values())
