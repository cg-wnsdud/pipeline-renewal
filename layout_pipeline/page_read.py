"""페이지 우선 판독(page/2): VLM 이 원본 페이지(또는 긴 이미지의 타일) 전체를 보고 기존 region_id 별 문구를 직접 쓴다.

- 결과 단위는 Stage 1 의 원시 레이아웃 영역(ID·bbox 그대로)이다. VLM 입력은 영역 crop 이 아니라 페이지 전체다.
- 위치 참조: 원본 이미지 1장(글자를 읽는 이미지) + 같은 이미지에 영역 상자·라벨만 그린 표시 사본 1장.
  표시가 글자를 가릴 수 있으므로 원본을 함께 보낸다(이미지 2장 비용은 호출 기록의 images 에 남는다).
- OCR 은 영역별 글자 후보(문자열 순 정렬, 순서 정보 없음)로만 준다. OCR 문자열을 정렬·접합해 최종 문구를 만들지 않는다.
- 표 후보는 같은 페이지 요청 안에서 표 여부·행을 함께 받는다(tables 배열).
- 영역 추가 확인(region-verify/1: 영역 원본 crop + 1차 페이지 판독 문구)은 아래 일반 조건에 해당하는 영역만 보낸다.
  경계·겹침 같은 맥락 근거만으로는 보내지 않는다. 파일·영역 ID 별 분기는 없다.
"""
from __future__ import annotations

import json
import re
import statistics

from PIL import Image, ImageDraw, ImageFont

from . import vlm_tasks
from .text_eval import digits, norm
from .vlm_client import server_resize

PAGE_VERSION = "page/2"
PAGE_VERSION_NO_OCR = "page/2-noocr"
VERIFY_VERSION = "region-verify/1"
PAGE_MAX_TOKENS = 12000
# 페이지 판독 출력 한도(page-tokens/1, docs/20): 판독 단위 OCR 글자 수에 비례. 고정 12,000 일 때 정상 응답 345건의 최대 출력은
# 3,844토큰이었고(이 식의 한도를 넘는 정상 응답 0건), 반복 퇴화 응답 6건은 12,000까지 가며 건마다 160~250초를 썼다.
PAGE_TOKENS_RULE = {"name": "page-tokens/1", "base": 1500, "per_ocr_char": 2.5, "min": 6000, "max": PAGE_MAX_TOKENS}
# 잘린 페이지 응답에서 완성된 영역 항목만 쓴다(page-partial/1). 이 표시가 있는 요청 기록에만 적용해 이전 실행 재구성은 그대로다.
PAGE_PARTIAL_VERSION = "page-partial/1"
VERIFY_MAX_TOKENS = 8000

# 영역 추가 확인 조건(실행 전 고정, docs/08). 모두 일반 조건이다.
# 2026-10-26 1회 수정(docs/08 A 결과): 12 → 18. 작은 비교에서 14~18px 구간 영역에 페이지 판독만의 오독이 있었다.
MIN_EFFECTIVE_TEXT_PX = 18      # 서버 리사이즈 뒤 영역 OCR 글자 상자 높이 중앙값(px) 추정치가 이보다 작으면 crop 확인
BOUNDARY_REF_MIN_INSIDE = 0.5   # 경계 OCR 도 영역 안 비율이 이 이상이면 그 영역 글자 근거로 본다
MISSING_OCR_COVER = 0.85        # 영역 안 OCR 줄이 1차 판독에 덮이는 비율이 이보다 낮으면(누락 근거) crop 확인
FONT = vlm_tasks.FONT
COLORS = vlm_tasks.COLORS


PAGE_RULES = (
    "첫 번째 이미지는 원본입니다. 글자는 이 이미지에서 읽습니다. 두 번째 이미지는 같은 이미지에 판독 대상 영역을 색 상자와 "
    "라벨(L 번호)로 표시한 사본이며 위치 확인용입니다(표시가 글자를 가릴 수 있음). 영역 목록의 bbox 는 원본 이미지 픽셀 좌표"
    "(x0,y0,x1,y1)입니다.\n"
    "할 일: 영역 목록의 모든 region_id 에 대해, 그 상자 안에 보이는 모든 문구를 원본 이미지에서 직접 읽어 text 로 씁니다. "
    "이미지 전체를 보고 각 문구가 어떤 제목·설명·항목·값·도해에 속하는지 판단한 뒤 그 관계가 드러나게 씁니다.\n"
    "[영역]\n"
    "- 각 영역 text 에는 그 상자 안의 문구만 씁니다. 다른 상자나 상자 밖의 문구는 관계를 설명하기 위해서라도 넣지 않습니다.\n"
    "- 상자 경계에 걸친 글자 줄은 대부분이 상자 안에 있으면 그 영역에 씁니다.\n"
    "- 상자 안의 문구는 하나도 빠뜨리지 않습니다. 설명 문단, 도해·비교 그림 속 글자, 로고·머리글·배지·버튼·손글씨, 각주를 "
    "모두 씁니다. 도해를 정리하느라 같은 상자 안의 설명 문단을 빼지 않습니다.\n"
    "- 상자끼리 겹치면 겹친 곳의 문구는 해당하는 각 영역에 씁니다.\n"
    "[읽기 순서와 관계]\n"
    "- 순서는 이미지의 시각적 배치로 정합니다. OCR 후보 목록의 순서는 읽기 순서가 아닙니다.\n"
    "- 제목 다음에 그 설명을 씁니다. 좌우로 나뉜 블록(단·비교·시나리오)은 한 블록을 끝까지 쓴 뒤 다음 블록을 씁니다. "
    "서로 다른 블록의 줄을 한 줄로 섞지 않습니다.\n"
    "- 항목(라벨)과 값이 좌우로 짝지어 있으면 위 행부터 차례로 '항목 값'을 씁니다. 항목명이 여러 줄이면 항목명 줄을 모두 "
    "이어 하나의 항목명으로 쓴 다음 그 값을 씁니다. 값이 여러 줄이면 그 항목 바로 뒤에 이어 씁니다. 항목명 줄과 값 줄을 "
    "번갈아 섞지 않습니다.\n"
    "- 라벨이 한 줄로 늘어서고 그 아래에 값이 있으면 라벨마다 그 아래 값을 짝지어 씁니다.\n"
    "- 도해는 시각적으로 이어진 요소끼리(구분 → 조건 → 수치) 묶어서 씁니다.\n"
    "- 줄이 바뀌는 곳은 줄바꿈합니다. 같은 위치의 문구를 두 번 쓰지 않고, 실제로 여러 번 나오는 문구는 그 횟수대로 씁니다.\n"
    "[글자]\n"
    "- 이미지에 보이는 표기를 그대로 씁니다. 맞춤법·조사가 어색해 보여도 원본 표기를 바꾸지 않습니다. 금액·금리·기간·날짜·"
    "상품명을 상품 지식이나 흔한 값으로 채우거나 바꾸지 않습니다.\n"
    "- 이미지에 없는 제목·설명·연결어·인과관계를 만들지 않고, 요약·번역하지 않습니다.\n"
)
OCR_RULES = (
    "- OCR 후보는 같은 영역에서 OCR 이 읽은 글자 조각입니다(문자열 순 정렬, 틀릴 수 있음). 빠뜨린 문구가 없는지 확인하는 "
    "데만 쓰고 후보를 나열하거나 옮겨 적지 않습니다. 후보 글자가 이미지와 다르면 이미지대로 씁니다. 그림·무늬를 OCR 이 "
    "글자로 읽은 후보도 있으니 이미지에서 글자로 보이지 않으면 쓰지 않습니다. boundary 후보의 inside_ratio 는 그 OCR "
    "글자 상자가 영역 안에 들어온 비율입니다.\n"
)
TAIL_RULES = (
    "[불확실] 글자가 작거나 흐려 확신할 수 없거나, 어느 블록·항목에 속하는지 불명확하면 uncertain=true. 상자 안에 글자가 "
    "없으면 빈 문자열, 판독할 수 없으면 null.\n"
    "[표 후보] tables 에는 table_candidate=true 인 영역만 씁니다. is_table: 행과 열, 또는 항목-조건-값이 서로 대응하는 실제 "
    "표이면 true, 메뉴·탭·버튼 나열이나 단순 문단이면 false. rows: is_table=true 이면 표의 모든 행(머리글·주석 행 포함)을 "
    "위에서 아래로, 각 행은 왼쪽→오른쪽 셀 문자열 배열로 씁니다. 셀 안 여러 줄은 공백으로 잇고 병합 셀은 시작 행에만 씁니다. "
    "is_table=false 이면 빈 배열. 표 후보 영역의 text 에도 그 상자의 모든 문구를 씁니다(rows 와 겹쳐도 비우지 않음).\n"
)


def view_regions(record: dict, view: dict) -> list[dict]:
    """판독 단위의 VLM 판독 대상 영역. HWP 구조 Region(text_source, 문구 = HWP 원문)은 읽지 않는다(docs/20)."""
    return [r for r in record["regions"] if r["view_id"] == view["view_id"] and not r.get("text_source")]


def _to_view(bbox, view: dict) -> list[int]:
    x0, y0 = view["crop"][0], view["crop"][1]
    return [round(bbox[0] - x0), round(bbox[1] - y0), round(bbox[2] - x0), round(bbox[3] - y0)]


def marked_copy(img: Image.Image, items: list[dict]) -> Image.Image:
    """영역 상자와 라벨만 그린 사본. 라벨은 가능하면 상자 위 바깥에 둔다(글자를 덜 가리게)."""
    marked = img.copy()
    d = ImageDraw.Draw(marked)
    lw = max(2, img.width // 600)
    font = ImageFont.truetype(FONT, max(14, img.width // 80))
    for k, it in enumerate(items):
        color = COLORS[k % len(COLORS)]
        b = it["bbox_px"]
        d.rectangle(b, outline=color, width=lw)
        tw = d.textlength(it["label"], font=font)
        ty = b[1] - font.size - 4 if b[1] - font.size - 4 >= 0 else b[1] + lw
        d.rectangle([b[0], ty, b[0] + tw + 6, ty + font.size + 3], fill=color)
        d.text((b[0] + 3, ty), it["label"], font=font, fill=(255, 255, 255))
    return marked


def page_schema(ids: list[str], table_ids: list[str]) -> dict:
    reg = {"type": "object", "additionalProperties": False, "required": ["region_id", "text", "uncertain"],
           "properties": {"region_id": {"type": "string", "enum": ids}, "text": {"type": ["string", "null"]},
                          "uncertain": {"type": "boolean"}}}
    props = {"regions": {"type": "array", "items": reg, "minItems": len(ids), "maxItems": len(ids)}}
    req = ["regions"]
    if table_ids:
        tab = {"type": "object", "additionalProperties": False, "required": ["region_id", "is_table", "rows"],
               "properties": {"region_id": {"type": "string", "enum": table_ids}, "is_table": {"type": "boolean"},
                              "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}}
        props["tables"] = {"type": "array", "items": tab, "minItems": len(table_ids), "maxItems": len(table_ids)}
        req.append("tables")
    return {"type": "object", "additionalProperties": False, "required": req, "properties": props}


def page_request(record: dict, view: dict, page_img: Image.Image, with_ocr: bool = True,
                 regions: list[dict] | None = None) -> dict:
    """원본(페이지·타일·VLM 입력 띠) + 표시 사본 + 영역 목록(ID·bbox·표 후보·OCR 후보)."""
    img = page_img.crop(tuple(view["crop"]))
    regions = view_regions(record, view) if regions is None else regions
    items = []
    for r in regions:
        it = {"region_id": r["region_id"], "label": vlm_tasks.short(r["region_id"]), "bbox_px": _to_view(r["bbox"], view),
              "table_candidate": r["table_candidate"]}
        if with_ocr:
            it |= vlm_tasks.candidate_context(record, r, "crop/3")
        items.append(it)
    ids = [r["region_id"] for r in regions]
    table_ids = [r["region_id"] for r in regions if r["table_candidate"]]
    w, h = img.size
    text = (PAGE_RULES + (OCR_RULES if with_ocr else "") + TAIL_RULES
            + f"원본 이미지 크기: {w}x{h}px. 영역 목록:\n" + json.dumps(items, ensure_ascii=False))
    return {"mode": "page2", "version": PAGE_VERSION if with_ocr else PAGE_VERSION_NO_OCR, "partial_salvage": PAGE_PARTIAL_VERSION,
            "view_id": view["view_id"],
            "region_ids": ids, "table_ids": table_ids, "images": [img, marked_copy(img, items)],
            "image_roles": [f"view_original:{view['view_id']}", f"view_marked_copy:{view['view_id']}"],
            "text": text, "schema": page_schema(ids, table_ids), "items": items}


# (나) page-band/1: 서버 리사이즈 배율이 이보다 낮은 페이지는 VLM 입력만 영역을 가로지르지 않는 위치에서 나눈다.
BAND_SPLIT_BELOW = 0.7
BAND_TARGET_FACTOR = 0.8


def vlm_bands(view: dict, regions: list[dict], budget: int = 2_580_480) -> dict:
    """VLM 입력 띠. OCR·영역은 바꾸지 않는다. 영역 bbox 를 가로지르지 않는 가로 위치(영역 사이 빈 곳)에서만 자른다.

    각 띠의 리사이즈 배율이 BAND_TARGET_FACTOR 이상이 되도록 띠 수를 최소로 정하고, 같은 수면 넓은 빈 곳을 고른다.
    자를 위치가 없으면 나누지 않고 사유를 남긴다."""
    x0, y0, x1, y1 = view["crop"]
    w, h = x1 - x0, y1 - y0
    factor = server_resize((w, h))["factor"]
    if factor >= BAND_SPLIT_BELOW or not regions:
        return {"split": False, "factor": round(factor, 3), "bands": [[y0, y1]]}
    max_h = budget / (BAND_TARGET_FACTOR ** 2 * w)
    spans = sorted((max(y0, r["bbox"][1]), min(y1, r["bbox"][3])) for r in regions)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = [((merged[i][1] + merged[i + 1][0]) / 2, merged[i + 1][0] - merged[i][1]) for i in range(len(merged) - 1)]
    nodes = [(y0, 0.0)] + [(round(g), gap) for g, gap in gaps] + [(y1, 0.0)]
    best = {0: (0, 0.0, None)}            # 노드 → (띠 수, -빈 곳 합, 이전 노드)
    for i in range(len(nodes)):
        if i not in best:
            continue
        for j in range(i + 1, len(nodes)):
            if nodes[j][0] - nodes[i][0] > max_h and j > i + 1:
                break
            cand = (best[i][0] + 1, best[i][1] - nodes[j][1], i)
            if j not in best or cand[:2] < best[j][:2]:
                best[j] = cand
    path, k = [], len(nodes) - 1
    while k is not None:
        path.append(nodes[k][0])
        k = best[k][2]
    cuts = sorted(path)
    bands = [[a, b] for a, b in zip(cuts, cuts[1:])]
    return {"split": len(bands) > 1, "factor": round(factor, 3), "bands": bands, "max_band_px": round(max_h),
            "oversized": [b for b in bands if b[1] - b[0] > max_h],
            "note": None if len(bands) > 1 else "영역을 가로지르지 않는 자를 위치가 없어 나누지 않음"}


def band_views(view: dict, regions: list[dict]) -> list[tuple[dict, list[dict]]]:
    """판독 단위(띠)와 그 안의 영역. 나누지 않으면 원래 view 하나."""
    plan = vlm_bands(view, regions)
    if not plan["split"]:
        return [(view, regions)]
    out = []
    for k, (a, b) in enumerate(plan["bands"]):
        bv = view | {"view_id": f"{view['view_id']}-b{k}", "crop": [view["crop"][0], a, view["crop"][2], b], "band_of": view["view_id"],
                     "band_plan": {k2: v for k2, v in plan.items() if k2 != "bands"}}
        out.append((bv, [r for r in regions if a <= r["bbox"][1] and r["bbox"][3] <= b + 0.5]))
    return out


def page_max_tokens(record: dict, regions: list[dict]) -> int:
    """판독 단위의 출력 한도(PAGE_TOKENS_RULE): base + per_ocr_char × 그 단위 영역들의 OCR 글자 수, [min, max]."""
    r = PAGE_TOKENS_RULE
    chars = sum(len(o["ocr_text"].strip()) for reg in regions for o in region_ocr(record, reg))
    return int(max(r["min"], min(r["max"], r["base"] + r["per_ocr_char"] * chars)))


_PARTIAL_HEAD = re.compile(r'\s*\{\s*"regions"\s*:\s*\[')
_PARTIAL_SEP = re.compile(r'\s*,?\s*')


def salvage_regions(content: str | None) -> list[dict]:
    """출력 한도에서 잘린 page2 응답의 regions 배열에서 끝까지 쓴 항목만(앞에서부터). 반복 퇴화는 마지막 항목 안에서 잘린다."""
    head = _PARTIAL_HEAD.match(content or "")
    if not head:
        return []
    dec, i, out = json.JSONDecoder(), head.end(), []
    while True:
        i = _PARTIAL_SEP.match(content, i).end()
        try:
            item, i = dec.raw_decode(content, i)
        except ValueError:
            return out
        if isinstance(item, dict):
            out.append(item)


def validate_page(call, request: dict) -> dict[str, dict]:
    """page2 응답 계약 검사. region_id → {text, uncertain, is_table, rows, contract_errors}.
    page-partial/1 요청이 출력 한도에서 잘렸으면 끝까지 쓴 영역 항목은 쓰고(page_output_partial) 나머지 영역만 실패로 둔다."""
    ids, table_ids = request["region_ids"], request["table_ids"]
    out = {i: {"region_id": i, "text": None, "uncertain": None, "contract_errors": []} for i in ids}
    errors = [call.error] if call.error else []
    parsed = call.parsed if isinstance(call.parsed, dict) else {}
    partial_err = []
    if (not parsed and request.get("partial_salvage") == PAGE_PARTIAL_VERSION
            and getattr(call, "finish_reason", None) == "length"):
        items = [r for r in salvage_regions(getattr(call, "content", None)) if r.get("region_id") in out]
        if items:
            parsed, partial_err, errors = {"regions": items}, errors, []
            for r in items:
                out[r["region_id"]]["page_output_partial"] = True
    regs = parsed.get("regions")
    if not isinstance(regs, list):
        errors.append("regions 배열 없음")
        regs = []
    seen = [r.get("region_id") for r in regs if isinstance(r, dict)]
    # ID 누락·중복은 해당 영역만 실패로 본다(페이지 전체를 실패로 돌리면 정상 응답한 영역까지 다시 읽게 된다).
    missing = set(ids) - set(seen)
    dup = {x for x in seen if seen.count(x) > 1}
    for i in missing:
        out[i]["contract_errors"] += partial_err + ["페이지 응답에 없음"]
    for i in dup & set(ids):
        out[i]["contract_errors"].append("페이지 응답에 중복")
    if set(seen) - set(ids):
        errors.append(f"요청에 없는 region_id {sorted(set(seen) - set(ids))}")
    for r in regs:
        if not isinstance(r, dict) or r.get("region_id") not in out or r.get("region_id") in dup:
            continue
        cur = out[r["region_id"]]
        text = r.get("text")
        if text is not None and not isinstance(text, str):
            cur["contract_errors"].append(f"text 타입 {type(text).__name__}")
            text = None
        if not isinstance(r.get("uncertain"), bool):
            cur["contract_errors"].append(f"uncertain 타입 {type(r.get('uncertain')).__name__}")
        cur.update(text=text, uncertain=r.get("uncertain") if isinstance(r.get("uncertain"), bool) else None)
    tabs = parsed.get("tables") if table_ids else []
    tabs = tabs if isinstance(tabs, list) else []
    tseen = [t.get("region_id") for t in tabs if isinstance(t, dict)]
    for tid in table_ids:
        hits = [t for t in tabs if isinstance(t, dict) and t.get("region_id") == tid]
        if len(hits) != 1:
            out[tid]["contract_errors"].append(f"tables 항목 {len(hits)}개")
            out[tid].update(is_table=None, rows=None)
            continue
        t = hits[0]
        rows = t.get("rows")
        ok_rows = isinstance(rows, list) and all(isinstance(x, list) and all(isinstance(c, str) for c in x) for x in rows)
        if not isinstance(t.get("is_table"), bool):
            out[tid]["contract_errors"].append("is_table 타입")
        if not ok_rows:
            out[tid]["contract_errors"].append("rows 형식 오류")
        out[tid].update(is_table=t.get("is_table") if isinstance(t.get("is_table"), bool) else None,
                        rows=rows if ok_rows else None)
    extra = sorted(set(tseen) - set(table_ids))
    if extra:
        errors.append(f"표 후보가 아닌 tables 항목 {extra}")
    for i in ids:
        out[i]["contract_errors"] = errors + out[i]["contract_errors"]
        out[i]["contract_ok"] = not out[i]["contract_errors"]
    return out


def region_ocr(record: dict, region: dict) -> list[dict]:
    """영역의 OCR 근거: 배정 OCR + 영역 안 비율 0.5 이상인 경계(미배정) OCR. 엄격 포함 배정만 쓰면 경계에 걸친 영역은 근거가 비어
    확인 조건에서 빠진다(작은 비교의 L014·L025·L026)."""
    rid = region["region_id"]
    out = [a for a in record["assignments"] if a["region_id"] == rid and a["ocr_text"].strip()
           and "low_ocr_overlap" not in a.get("assignment_review", [])]
    for u in record["unassigned_texts"]:
        if u["ocr_text"].strip() and any(c["region_id"] == rid and c["inside_ratio"] >= BOUNDARY_REF_MIN_INSIDE
                                         for c in u["candidates"]):
            out.append(u)
    return out


def effective_text_px(record: dict, region: dict, view: dict) -> float | None:
    """서버 이미지 처리기 리사이즈 뒤의 영역 글자 높이 추정(영역 OCR 근거 상자 높이 중앙값 × 배율). 확대(배율>1)는 1로 본다."""
    hs = [o["ocr_bbox"][3] - o["ocr_bbox"][1] for o in region_ocr(record, region)]
    if not hs:
        return None
    w, h = view["crop"][2] - view["crop"][0], view["crop"][3] - view["crop"][1]
    return round(statistics.median(hs) * min(1.0, server_resize((w, h))["factor"]), 1)


def verify_reasons(record: dict, region: dict, view: dict, res: dict) -> list[str]:
    """영역 추가 확인 조건. overlap-best/1 의 약한 겹침·복수 영역 경합은 배정 불확실성으로 확인한다."""
    reasons = []
    if any(a["region_id"] == region["region_id"] and "low_ocr_overlap" in a.get("assignment_review", [])
           for a in record["assignments"]):
        reasons.append("low_ocr_overlap_assigned")
    if any(a["region_id"] == region["region_id"] and "multiple_region_overlap" in a.get("assignment_review", [])
           for a in record["assignments"]):
        reasons.append("multiple_region_overlap_assigned")
    if not res.get("contract_ok") or res.get("text") is None:
        reasons.append("page_output_failed")
    if res.get("uncertain") is True:
        reasons.append("vlm_uncertain")
    eff = effective_text_px(record, region, view)
    if eff is not None and eff < MIN_EFFECTIVE_TEXT_PX:
        reasons.append("small_text_after_resize")
    text = res.get("text")
    if text is not None:
        own = region_ocr(record, region)
        lines = [o["ocr_text"] for o in own]
        if outside_text(record, region, text, {o["ocr_id"] for o in own}):
            reasons.append("text_from_outside_box")
        if order_conflict(record, region, text):
            reasons.append("reading_order_conflict")
        if repeated_text(lines, text):
            reasons.append("repeated_text_in_page_read")
        if norm("".join(lines)):
            ocr_in_vlm, _ = vlm_tasks.ocr_vlm_agreement(lines, text)
            if ocr_in_vlm < MISSING_OCR_COVER:
                reasons.append("ocr_text_missing_in_page_read")
            missing_digits = set(digits("\n".join(lines))) - set(digits(text))
            if missing_digits:
                reasons.append("ocr_digits_missing_in_page_read")
    if region["table_candidate"] and res.get("contract_ok"):
        rows = res.get("rows")
        if res.get("is_table") is True and not rows:
            reasons.append("table_rows_missing")
        elif res.get("is_table") is True and text:
            rows_text = "\n".join(" ".join(r) for r in rows)
            from .text_eval import similarity_cover
            if similarity_cover(text, rows_text) < MISSING_OCR_COVER:
                reasons.append("table_rows_incomplete")
    return reasons


# simple-column-order/1(docs/11 §4): 영역 OCR 근거가 한 단으로 쌓인 줄일 때만, 판독 문구의 줄 순서가 위→아래와 어긋나는지 본다.
# 문구를 고치거나 OCR 순서로 조립하지 않는다. 어긋나면 영역 재판독(시각 확인)으로 보내는 조건으로만 쓴다.
ORDER_CHECK_VERSION = "simple-column-order/1"
SAME_LINE_OVERLAP = 0.5     # 두 OCR 상자의 세로 겹침이 작은 쪽 높이의 이 비율 이상이면 같은 줄
LINE_GAP_MAX = 1.5          # 같은 줄 안 OCR 상자 사이 가로 간격이 줄 높이의 이 배수를 넘으면 단이 나뉜 배치로 보고 검사하지 않음


def simple_column_lines(ocrs: list[dict]) -> list[list[dict]] | None:
    """OCR 상자가 한 단(열)으로 위에서 아래로 쌓인 줄이면 줄 목록(위→아래, 줄 안은 왼→오른쪽), 아니면 None.
    같은 높이에 멀리 떨어진 상자가 있으면(좌우 블록·항목–값 배치·도해) 단순 세로 배치가 아니다."""
    rows: list[dict] = []
    for o in sorted(ocrs, key=lambda o: (o["ocr_bbox"][1] + o["ocr_bbox"][3]) / 2):
        b = o["ocr_bbox"]
        for row in rows:
            top, bot = row["span"]
            if min(b[3], bot) - max(b[1], top) >= SAME_LINE_OVERLAP * min(b[3] - b[1], bot - top):
                row["items"].append(o)
                row["span"] = [min(top, b[1]), max(bot, b[3])]
                break
        else:
            rows.append({"span": [b[1], b[3]], "items": [o]})
    out = []
    for row in sorted(rows, key=lambda r: r["span"][0]):
        items = sorted(row["items"], key=lambda o: o["ocr_bbox"][0])
        h = row["span"][1] - row["span"][0]
        if any(nxt["ocr_bbox"][0] - cur["ocr_bbox"][2] > LINE_GAP_MAX * h for cur, nxt in zip(items, items[1:])):
            return None
        out.append(items)
    return out


def order_conflict(record: dict, region: dict, text: str) -> dict | None:
    """단순 세로 배치 영역에서 판독 문구의 줄 순서가 위→아래 순서와 어긋나면 근거를 돌려준다(표 후보 제외).
    각 OCR 조각은 판독 문구(정규화)에 정확히 한 번 나올 때만 위치로 쓴다."""
    if region["table_candidate"] or not text:
        return None
    lines = simple_column_lines([o for o in region_ocr(record, region) if len(norm(o["ocr_text"])) >= 2])
    if not lines or len(lines) < 2:
        return None
    body = norm(text)
    pos = []
    for row in lines:
        hits = [body.index(norm(o["ocr_text"])) for o in row if body.count(norm(o["ocr_text"])) == 1]
        if hits:
            pos.append(min(hits))
    inv = sum(b < a for a, b in zip(pos, pos[1:]))
    return {"version": ORDER_CHECK_VERSION, "lines": len(lines), "located": len(pos), "inversions": inv} if inv else None


def repeated_text(ocr_lines: list[str], text: str) -> list[str]:
    """1차 판독에서 OCR 근거보다 더 많이 반복된 글자 조각(정규화 4자 이상). 같은 위치를 두 번 쓴 근거다(L022형)."""
    body, joined = norm(text), norm("".join(ocr_lines))
    return sorted({n for n in (norm(x) for x in ocr_lines) if len(n) >= 4 and body.count(n) > max(1, joined.count(n))})


def outside_text(record: dict, region: dict, text: str, own_ids: set[str]) -> list[str]:
    """1차 판독 문구에 상자와 전혀 겹치지 않는 OCR 줄(정규화 6자 이상)이 그대로 들어 있으면 그 OCR 원문을 돌려준다.
    다른 영역 문구를 관계 설명으로 끌어온 근거다(L014형). 같은 글자가 상자와 겹치는 OCR 에도 있으면(실제 반복 문구:
    좌우 상품 칸의 같은 각주, 경로·제목의 같은 글자 등) 세지 않는다."""
    from .geometry import intersection
    body = norm(text)
    items = record["assignments"] + record["unassigned_texts"]
    near = [norm(o["ocr_text"]) for o in items if o["ocr_id"] in own_ids or intersection(o["ocr_bbox"], region["bbox"]) > 0]
    hits = []
    for o in items:
        n = norm(o["ocr_text"])
        if o["ocr_id"] in own_ids or len(n) < 6 or intersection(o["ocr_bbox"], region["bbox"]) > 0:
            continue
        if n in body and not any(n in x or (len(x) >= 6 and x in n) for x in near):
            hits.append(o["ocr_text"])
    return hits


VERIFY_RULES = (
    "첨부 이미지는 순서대로 아래 영역의 원본 crop 입니다(이미지 k = image_index k). page_text 는 같은 영역을 페이지 전체에서 "
    "읽은 1차 판독입니다(null 이면 1차 판독 없음). 각 region_id 에 대해 crop 을 글자 단위로 대조해 최종 문구를 쓰세요.\n"
    "- 1차 판독의 줄·항목 순서(읽기 순서, 여러 줄 항목명과 그 값의 연결, 블록 구분)는 crop 의 배치와 맞으면 그대로 유지합니다.\n"
    "- crop 에 보이는데 1차 판독에 없는 문구는 알맞은 위치에 넣고, 잘못 읽은 글자는 crop 대로 고칩니다.\n"
    "- crop 에 보이지 않는 글자는 쓰지 않습니다. crop 가장자리에 걸친 글자 줄은 대부분이 보이면 씁니다.\n"
    "- 이미지에 보이는 표기를 그대로 씁니다(맞춤법·조사 교정, 상품 지식으로 값 변경, 요약 금지). OCR 후보는 빠진 문구 확인용이며 "
    "나열하거나 옮겨 적지 않습니다.\n"
    "- 확신할 수 없으면 uncertain=true, 판독할 수 없으면 null.\n"
)


def verify_request(record: dict, regions: list[dict], page_img: Image.Image, page_texts: dict[str, str | None]) -> dict:
    images, items = [], []
    for k, r in enumerate(regions, 1):
        box = vlm_tasks.crop_box(r["bbox"], page_img.width, page_img.height)
        images.append(page_img.crop(box))
        items.append({"image_index": k, "region_id": r["region_id"], "crop_px": box,
                      "page_text": page_texts.get(r["region_id"])} | vlm_tasks.candidate_context(record, r, "crop/3"))
    ids = [r["region_id"] for r in regions]
    return {"mode": "crop", "version": VERIFY_VERSION, "region_ids": ids, "images": images,
            "image_roles": [f"region_crop:{i}" for i in ids], "text": VERIFY_RULES + "영역 목록:\n" + json.dumps(items, ensure_ascii=False),
            "schema": vlm_tasks.read_schema(ids), "items": items}


# (가) region-patch/1: 영역 확인은 1차 페이지 판독을 다시 쓰지 않고 고칠 점만 돌려준다. 반영은 코드가 한다.
PATCH_VERSION = "region-patch/1"
PATCH_RULES = (
    "첨부 이미지는 순서대로 아래 영역의 원본 crop 입니다(이미지 k = image_index k). page_text 는 같은 영역을 페이지 전체에서 "
    "읽은 1차 판독입니다. 1차 판독의 줄 순서·항목–값 연결·블록 구분은 페이지 전체를 보고 정한 것이므로 다시 쓰지 않습니다. "
    "crop 과 글자 단위로 대조해 고칠 점만 돌려주세요.\n"
    "- missing: crop 에 보이는데 1차 판독에 없는 문구. text = 빠진 문구, after = 그 문구 바로 앞에 오는 1차 판독의 문자열"
    "(1차 판독에 있는 그대로, 한 번만 나오도록. 맨 앞이면 빈 문자열).\n"
    "- corrections: 1차 판독에서 잘못 읽은 글자. wrong = 1차 판독에 있는 그대로의 문자열(1차 판독 안에서 한 번만 나오도록 앞뒤 "
    "글자를 포함), right = crop 에 보이는 대로 고친 문자열. 같은 위치를 두 번 쓴 부분도 corrections 로 고칩니다"
    "(wrong = 중복이 들어 있는 부분, right = 중복을 뺀 부분).\n"
    "- remove: 1차 판독에 있지만 crop 에 보이지 않는 문구(다른 영역의 문구). 1차 판독에 있는 그대로, 한 번만 나오도록 씁니다.\n"
    "- 고칠 것이 없으면 세 목록을 모두 빈 배열로 둡니다. 원본 표기는 맞춤법이 어색해도 고치지 않고, 상품 지식으로 값을 바꾸지 "
    "않습니다. OCR 후보는 빠진 문구 확인용이며 그대로 옮기지 않습니다.\n"
    "- 확신할 수 없으면 uncertain=true.\n")


def patch_schema(ids: list[str]) -> dict:
    s_ = {"type": "string"}
    item = {"type": "object", "additionalProperties": False,
            "required": ["region_id", "missing", "corrections", "remove", "uncertain"],
            "properties": {"region_id": {"type": "string", "enum": ids},
                           "missing": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                  "required": ["text", "after"], "properties": {"text": s_, "after": s_}}},
                           "corrections": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                      "required": ["wrong", "right"], "properties": {"wrong": s_, "right": s_}}},
                           "remove": {"type": "array", "items": s_}, "uncertain": {"type": "boolean"}}}
    return {"type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": item, "minItems": len(ids), "maxItems": len(ids)}}}


def patch_request(record: dict, regions: list[dict], page_img: Image.Image, page_texts: dict[str, str]) -> dict:
    images, items = [], []
    for k, r in enumerate(regions, 1):
        box = vlm_tasks.crop_box(r["bbox"], page_img.width, page_img.height)
        images.append(page_img.crop(box))
        items.append({"image_index": k, "region_id": r["region_id"], "crop_px": box,
                      "page_text": page_texts.get(r["region_id"])} | vlm_tasks.candidate_context(record, r, "crop/3"))
    ids = [r["region_id"] for r in regions]
    return {"mode": "patch", "version": PATCH_VERSION, "region_ids": ids, "images": images,
            "image_roles": [f"region_crop:{i}" for i in ids],
            "text": PATCH_RULES + "영역 목록:\n" + json.dumps(items, ensure_ascii=False), "schema": patch_schema(ids), "items": items}


def validate_patch(call, request: dict) -> dict[str, dict]:
    ids = request["region_ids"]
    out = {i: {"region_id": i, "patch": None, "uncertain": None, "contract_errors": []} for i in ids}
    errors = [call.error] if call.error else []
    results = call.parsed.get("results") if isinstance(call.parsed, dict) else None
    if not isinstance(results, list):
        errors.append("results 배열 없음")
        results = []
    seen = [r.get("region_id") for r in results if isinstance(r, dict)]
    for i in set(ids) - set(seen):
        out[i]["contract_errors"].append("응답에 없음")
    for r in results:
        if not isinstance(r, dict) or r.get("region_id") not in out or seen.count(r.get("region_id")) > 1:
            continue
        ok = all(isinstance(r.get(k), list) for k in ("missing", "corrections", "remove"))
        if not ok:
            out[r["region_id"]]["contract_errors"].append("patch 형식 오류")
            continue
        out[r["region_id"]].update(patch={k: r[k] for k in ("missing", "corrections", "remove")},
                                   uncertain=r.get("uncertain") if isinstance(r.get("uncertain"), bool) else None)
    for i in ids:
        out[i]["contract_errors"] = errors + out[i]["contract_errors"]
        out[i]["contract_ok"] = not out[i]["contract_errors"]
    return out


def _tidy(t: str) -> str:
    lines = [" ".join(x.split()) for x in t.split("\n")]
    return "\n".join(x for x in lines if x)


def apply_patch(text: str, patch: dict) -> tuple[str, list[dict], list[dict]]:
    """1차 판독에 VLM 이 돌려준 고칠 점을 반영한다. 기준 문자열이 1차 판독에 정확히 한 번 있을 때만 적용한다.

    순서: 빠진 문구 넣기(1차 판독의 after 기준) → 글자 교정 → 상자 밖 문구 삭제. 적용하지 못한 지시는 따로 돌려준다."""
    t, applied, failed = text, [], []
    for m in patch.get("missing") or []:
        add, after = (m.get("text") or "").strip(), m.get("after") or ""
        if not add:
            continue
        if norm(add) and norm(add) in norm(t):
            failed.append({"missing": m, "why": "이미 있음"})
            continue
        if after == "":
            t = add + "\n" + t
        elif t.count(after) == 1:
            i = t.index(after) + len(after)
            t = t[:i] + ("\n" if t[i:i + 1] in ("", "\n") else " ") + add + t[i:]
        else:
            failed.append({"missing": m, "why": f"after 가 1차 판독에 {t.count(after)}번"})
            continue
        applied.append({"missing": m})
    for c in patch.get("corrections") or []:
        w, r = c.get("wrong") or "", c.get("right") or ""
        if w and w != r and t.count(w) == 1:
            t = t.replace(w, r)
            applied.append({"correction": c})
        elif w != r:
            failed.append({"correction": c, "why": f"wrong 이 1차 판독에 {t.count(w) if w else 0}번"})
    for rm in patch.get("remove") or []:
        if rm and t.count(rm) == 1:
            t = t.replace(rm, "")
            applied.append({"remove": rm})
        elif rm:
            failed.append({"remove": rm, "why": f"1차 판독에 {t.count(rm)}번"})
    return _tidy(t), applied, failed


# region-reread/1(docs/10): 1차 구조를 고정하지 않고, 대상 영역 하나의 완결된 text 후보를 원본에서 다시 읽는다.
# region-reread/2(docs/20): changes 를 분류별 참/거짓 객체로 받는다. /1 의 배열(enum 반복 가능)은 문구를 다 쓴 뒤 같은 값을
# 끝없이 반복해 출력 한도에서 잘렸고(whole-test 재판독 21건 전부), 완성된 text 까지 버려졌다. /2 는 잘린 응답이라도 text 가
# 완성돼 있으면 그 값을 꺼내 같은 선택 정책으로 고른다(reread_text_salvaged).
REREAD_VERSION = "region-reread/2"
REREAD_VERSIONS = ("region-reread/1", REREAD_VERSION)      # 재구성·기본 프로필이 받는 기록
REREAD_SELECT_VERSION = "reread-select/2"   # select_reread 선택 정책(/1: 상자 안 OCR 덮임만, /2: + 상자 밖 문구 유입 거부)
REREAD_MAX_TOKENS = 4000
REREAD_CHANGES = ["outside_text_removed", "order_or_block_fixed", "label_value_fixed", "duplicate_removed",
                  "missing_added", "chars_fixed", "none"]
REREAD_CHANGE_KEYS = [c for c in REREAD_CHANGES if c != "none"]
CONFLICT_DROP = 0.05            # 선택 정책: 재판독의 OCR 근거 덮임이 1차보다 이만큼 넘게 낮고
CONFLICT_FLOOR = 0.85           # 동시에 이 값 미만이면 1차 유지 + reread_conflict

REREAD_RULES = (
    "할 일: 대상 영역 안의 모든 문구를 원본에서 직접 읽어 이 영역의 완결된 text 를 씁니다.\n"
    "- 출력은 대상 상자 안 문구만 씁니다. 문맥 이미지에서만 보이는 이웃 제목·문구는 넣지 않습니다.\n"
    "- 좌우로 나뉜 블록은 한 블록을 끝까지 쓴 뒤 다음 블록을 씁니다. 각 블록의 제목은 그 블록 맨 앞에 씁니다.\n"
    "- 항목명이 여러 줄이면 항목명을 끝까지 이어 쓴 뒤 그 값 전체를 씁니다. 항목명 줄과 값 줄을 섞지 않습니다.\n"
    "- 도해는 시각적으로 연결된 요소끼리(구분 → 조건 → 수치) 묶어 씁니다. 같은 위치의 문구를 두 번 쓰지 않고, 원본에 실제로 "
    "여러 위치에 있는 문구는 각 위치에 맞게 씁니다.\n"
    "- first_pass_text 는 페이지(또는 띠)를 보고 쓴 1차 초안이며 틀릴 수 있습니다(다른 영역 문구 유입, 블록·항목 섞임, 중복, "
    "누락, 오독). 원본과 다르면 원본대로 고치고, 원본과 맞는 내용은 보존합니다.\n"
    "- OCR 후보는 같은 영역의 OCR 글자 조각(순서 없음, 틀릴 수 있음)으로, 빠진 글자를 확인하는 참고입니다. 나열하거나 옮겨 적지 "
    "않으며, OCR 에 없다는 이유로 원본 글자를 버리지 않습니다.\n"
    "- 원본 표기·금액은 맞춤법이 어색해도 바꾸지 않고 상품 지식으로 채우지 않습니다. 요약·번역하지 않습니다. '항목명:' 같은 "
    "꼬리표를 붙이지 않고 원문 줄을 자연스러운 순서와 줄바꿈으로 씁니다.\n"
    "- changes: first_pass_text 대비 바뀐 점의 분류마다 true/false(바뀐 점이 없으면 모두 false). notes: 근거를 짧게.\n"
    "- 읽을 수 없거나 어느 영역 문구인지 불명확하면 uncertain=true 로 두고 notes 에 이유를 씁니다.\n")


def context_marked(img: Image.Image, target: list[int]) -> Image.Image:
    """문맥 이미지: 대상 상자 바깥을 반투명하게 어둡게 하고 상자 바깥 3px 에 테두리. 대상 상자 안 픽셀은 바꾸지 않는다."""
    from PIL import ImageEnhance
    x0, y0, x1, y1 = target
    out = ImageEnhance.Brightness(img).enhance(0.55)
    out.paste(img.crop((x0, y0, x1, y1)), (x0, y0))
    d = ImageDraw.Draw(out)
    lw = max(2, img.width // 500)
    d.rectangle([x0 - 3 - lw, y0 - 3 - lw, x1 + 3 + lw, y1 + 3 + lw], outline=(220, 0, 0), width=lw)
    return out


def reread_request(record: dict, region: dict, page_img: Image.Image, first_text: str | None,
                   context_view: dict | None = None) -> dict:
    """대상 영역 하나. A: 원본 crop 1장. B(context_view 지정): + 대상이 표시된 문맥 이미지 1장."""
    box = vlm_tasks.crop_box(region["bbox"], page_img.width, page_img.height)
    images = [page_img.crop(box)]
    roles = [f"region_crop:{region['region_id']}"]
    head = (f"[대상] region_id={region['region_id']}, 원본 좌표 bbox={box}(원본 이미지 {page_img.width}x{page_img.height}px).\n"
            "첫 번째 이미지는 이 대상 영역을 원본에서 그대로 자른 crop 입니다(표시 없음). 글자는 이 이미지에서 읽습니다.\n")
    if context_view is not None:
        cx0, cy0, cx1, cy1 = context_view["crop"]
        ctx = page_img.crop((cx0, cy0, cx1, cy1))
        tgt = [box[0] - cx0, box[1] - cy0, box[2] - cx0, box[3] - cy0]
        images.append(context_marked(ctx, tgt))
        roles.append(f"context_marked:{context_view['view_id']}")
        head += (f"두 번째 이미지는 이 영역이 들어 있는 원본 {'띠' if context_view.get('band_of') else '페이지/타일'}"
                 f"(원본 좌표 {[cx0, cy0, cx1, cy1]})이며, 대상 영역은 이 이미지 안 {tgt} 입니다. 대상 바깥을 어둡게 하고 "
                 "바깥에 빨간 테두리를 그렸습니다. 이 이미지는 대상의 위치와 주변 관계를 이해하는 용도이며, 여기서만 보이는 글자는 "
                 "쓰지 않습니다.\n")
    item = {"region_id": region["region_id"], "first_pass_text": first_text} | vlm_tasks.candidate_context(record, region, "crop/3")
    schema = {"type": "object", "additionalProperties": False,
              "required": ["region_id", "text", "uncertain", "changes", "notes"],
              "properties": {"region_id": {"type": "string", "enum": [region["region_id"]]}, "text": {"type": ["string", "null"]},
                             "uncertain": {"type": "boolean"},
                             "changes": {"type": "object", "additionalProperties": False, "required": REREAD_CHANGE_KEYS,
                                         "properties": {k: {"type": "boolean"} for k in REREAD_CHANGE_KEYS}},
                             "notes": {"type": "string"}}}
    return {"mode": "reread", "version": REREAD_VERSION, "condition": "B" if context_view is not None else "A",
            "region_ids": [region["region_id"]], "images": images, "image_roles": roles,
            "text": head + REREAD_RULES + "입력:\n" + json.dumps(item, ensure_ascii=False), "schema": schema, "items": [item]}


_SALVAGE_HEAD = re.compile(r'\{\s*"region_id"\s*:\s*')
_SALVAGE_TEXT = re.compile(r'\s*,\s*"text"\s*:\s*')


def _changes_list(changes) -> list[str] | None:
    """/2 객체(분류별 참/거짓) → /1 과 같은 목록(참인 분류, 없으면 ['none']). /1 목록은 그대로."""
    if isinstance(changes, dict):
        return [k for k in REREAD_CHANGE_KEYS if changes.get(k) is True] or ["none"]
    return changes


def salvage_text(content: str | None, rid: str) -> str | None:
    """출력 한도에서 잘린 재판독 응답에서 완성된 text 값만 꺼낸다(뒤 필드에서 잘린 경우). text 자체가 잘렸으면 None."""
    s = (content or "").lstrip()
    head = _SALVAGE_HEAD.match(s)
    if not head:
        return None
    dec = json.JSONDecoder()
    try:
        got, i = dec.raw_decode(s, head.end())
        key = _SALVAGE_TEXT.match(s, i)
        if got != rid or not key:
            return None
        text, _ = dec.raw_decode(s, key.end())
    except ValueError:
        return None
    return text if isinstance(text, str) else None


def validate_reread(call, request: dict) -> dict[str, dict]:
    rid = request["region_ids"][0]
    out = {"region_id": rid, "text": None, "uncertain": None, "changes": None, "notes": None, "contract_errors": []}
    p = call.parsed if isinstance(call.parsed, dict) else None
    salvaged = None
    if p is None and request.get("version") == REREAD_VERSION and getattr(call, "finish_reason", None) == "length":
        salvaged = salvage_text(getattr(call, "content", None), rid)
    if salvaged is not None:
        # 잘린 뒤쪽 필드(uncertain·changes·notes)는 모른다. 문구는 같은 선택 정책(OCR 근거·상자 밖 유입)으로 고른다.
        out.update(text=salvaged, salvaged=True, notes=call.error)
        out["contract_ok"] = True
        return {rid: out}
    if call.error:
        out["contract_errors"].append(call.error)
    if p is None:
        out["contract_errors"].append("응답 없음")
    elif p.get("region_id") != rid:
        out["contract_errors"].append(f"region_id 불일치 {p.get('region_id')}")
    else:
        text = p.get("text")
        if text is not None and not isinstance(text, str):
            out["contract_errors"].append("text 타입")
            text = None
        if not isinstance(p.get("uncertain"), bool):
            out["contract_errors"].append("uncertain 타입")
        out.update(text=text, uncertain=p.get("uncertain") if isinstance(p.get("uncertain"), bool) else None,
                   changes=_changes_list(p.get("changes")), notes=p.get("notes"))
    out["contract_ok"] = not out["contract_errors"]
    return {rid: out}


def ocr_cover(record: dict, region: dict, text: str | None) -> float | None:
    lines = [o["ocr_text"] for o in region_ocr(record, region)]
    if not norm("".join(lines)) or text is None:
        return None
    return vlm_tasks.ocr_vlm_agreement(lines, text)[0]


def select_reread(record: dict, region: dict, first_text: str | None, reread: dict) -> dict:
    """후보 선택 정책(docs/10 §1, 실행 전 고정 + reread-select/2). 정답 자료를 보지 않는다. 문구는 고치지 않고 후보 중 하나를 고른다.

    reread-select/2(docs/16 §5.2): 재판독이 1차 판독에 없던 상자 밖 OCR 줄(outside_text: 다른 영역의 글자)을 새로 가져오면 채택하지
    않고 1차 판독을 유지한다. 상자 안 OCR 덮임만 보던 /1 은 tt02 그림 영역이 문맥 이미지에서 페이지 전체 문구를 가져온 후보를
    채택했다. 상자 안 누락만 보충한 후보는 그대로 채택한다."""
    if not reread.get("contract_ok") or reread.get("text") is None:
        return {"text": first_text, "source": "first_pass", "review": ["reread_failed"]}
    own_ids = {o["ocr_id"] for o in region_ocr(record, region)}
    before = set(outside_text(record, region, first_text, own_ids)) if first_text else set()
    added = [h for h in outside_text(record, region, reread["text"], own_ids) if h not in before]
    if added:
        return {"text": first_text, "source": "first_pass", "review": ["reread_outside_text"],
                "measures": {"reread_outside_lines": added[:10], "reread_outside_line_count": len(added)}}
    c1, c2 = ocr_cover(record, region, first_text), ocr_cover(record, region, reread["text"])
    if c1 is not None and c2 is not None and c2 < c1 - CONFLICT_DROP and c2 < CONFLICT_FLOOR:
        return {"text": first_text, "source": "first_pass", "review": ["reread_conflict"],
                "measures": {"ocr_cover_first": round(c1, 3), "ocr_cover_reread": round(c2, 3)}}
    review = (["vlm_uncertain"] if reread.get("uncertain") else []) + (["reread_text_salvaged"] if reread.get("salvaged") else [])
    return {"text": reread["text"], "source": "reread", "review": review,
            "measures": {"ocr_cover_first": None if c1 is None else round(c1, 3), "ocr_cover_reread": None if c2 is None else round(c2, 3)}}
