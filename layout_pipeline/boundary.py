"""긴 이미지 자동 경계 실험(docs/boundary-experiment-plan.md). 결과는 candidate 계획으로만 저장한다.

탐색 OCR 은 문맥·보호 위치 확인용이며 최종 Region 이 아니다. 경계의 의미 안전은 이 모듈이 확정하지 않는다.
"""
from __future__ import annotations

import json
import statistics

from PIL import Image, ImageDraw

from .regions import parse_view
from .routing import InputView

SCOUT_ASPECT = 2.5          # 실험 정책값(서버 보장값 아님)
EDGE_PX = 2                 # 탐색 창 가장자리에 닿은 상자는 잘린 관측으로 제외
PAD_PX = 4                  # 보호 구간 위아래 여유
MIN_GAP_PX = 6              # 후보로 삼을 빈 띠 최소 높이
M1_GAP_RATIO = 2.0          # M1: 빈 띠 ≥ 2.0 × 줄 간격 중앙값이면 safe
CONTEXT_PX = 300            # M2 입력 crop: 후보선 위아래 범위
RELATIONS = ["independent_sections", "heading_and_body", "same_paragraph_or_sentence", "inside_table",
             "button_and_description", "list_or_condition_continuation", "unknown"]


def scout_windows(width: int, height: int) -> list[tuple[int, int, int, int]]:
    win = round(width * SCOUT_ASPECT)
    step = max(1, win // 2)
    starts = list(range(0, max(1, height - win + 1), step))
    if starts[-1] + win < height:
        starts.append(height - win)
    return [(0, y, width, min(height, y + win)) for y in starts]


def observed_boxes(pruned: dict, view: InputView, page_h: int) -> list[dict]:
    """탐색 응답에서 잘리지 않은 레이아웃·OCR 상자(페이지 좌표)."""
    regions, items, _ = parse_view(pruned, view)
    y0, y1 = view.crop[1], view.crop[3]
    out = []
    for kind, objs in (("layout", regions), ("ocr", items)):
        for o in objs:
            b = o.bbox if kind == "layout" else o.ocr_bbox
            if kind == "ocr" and not o.ocr_text.strip():
                continue
            if (y0 > 0 and b[1] - y0 <= EDGE_PX) or (y1 < page_h and y1 - b[3] <= EDGE_PX):
                continue
            out.append({"kind": kind, "view_id": view.view_id, "bbox": [round(v, 1) for v in b],
                        "text": getattr(o, "ocr_text", None), "label": getattr(o, "raw_label", None)})
    return out


def candidates(boxes: list[dict], height: int) -> tuple[list[dict], float]:
    """보호 구간에 들지 않는 빈 띠의 중앙 = 후보. 줄 간격 중앙값도 돌려준다."""
    blocked = [False] * height
    for b in boxes:
        for y in range(max(0, int(b["bbox"][1]) - PAD_PX), min(height, int(b["bbox"][3]) + PAD_PX + 1)):
            blocked[y] = True
    runs, start = [], None
    for y in range(1, height - 1):
        if not blocked[y] and start is None:
            start = y
        elif blocked[y] and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, height - 1))
    ocr = sorted({(round(b["bbox"][1]), round(b["bbox"][3])) for b in boxes if b["kind"] == "ocr"})
    gaps = [b0 - a1 for (a0, a1), (b0, b1) in zip(ocr, ocr[1:]) if b0 - a1 > 0]
    line_gap = statistics.median(gaps) if gaps else 10.0
    cands = [{"y": (a + b) // 2, "gap_px": b - a} for a, b in runs if b - a >= MIN_GAP_PX]
    return cands, line_gap


def context_items(boxes: list[dict], y: int) -> tuple[list[dict], list[dict]]:
    ocr = [b for b in boxes if b["kind"] == "ocr"]
    seen, uniq = set(), []
    for b in sorted(ocr, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        key = (round(b["bbox"][0] / 4), round(b["bbox"][1] / 4), b["text"])
        if key not in seen:
            seen.add(key)
            uniq.append(b)
    above = [b for b in uniq if b["bbox"][3] <= y and b["bbox"][3] >= y - CONTEXT_PX][-8:]
    below = [b for b in uniq if b["bbox"][1] >= y and b["bbox"][1] <= y + CONTEXT_PX][:8]
    return ([{"id": f"A{i + 1}", "text": b["text"], "bbox": b["bbox"]} for i, b in enumerate(reversed(above))],
            [{"id": f"B{i + 1}", "text": b["text"], "bbox": b["bbox"]} for i, b in enumerate(below)])


def relation_request(page_img: Image.Image, y: int, above: list[dict], below: list[dict]) -> dict:
    top, bot = max(0, y - CONTEXT_PX), min(page_img.height, y + CONTEXT_PX)
    crop = page_img.crop((0, top, page_img.width, bot))
    marked = crop.copy()
    ImageDraw.Draw(marked).line((0, y - top, crop.width, y - top), fill=(220, 0, 0), width=3)
    a_ids = [a["id"] for a in above] + ["NONE"]
    b_ids = [b["id"] for b in below] + ["NONE"]
    schema = {"type": "object", "additionalProperties": False, "required": ["above_id", "below_id", "relation", "reason"],
              "properties": {"above_id": {"type": "string", "enum": a_ids}, "below_id": {"type": "string", "enum": b_ids},
                             "relation": {"type": "string", "enum": RELATIONS}, "reason": {"type": "string"}}}
    text = ("긴 광고 화면을 가로로 나눌 위치를 검토합니다. 첫 이미지는 후보선 주변 원본이고, 둘째는 같은 이미지에 후보선(빨간 선) 하나만 "
            "그린 사본입니다. 글자는 첫 이미지에서 읽으세요.\n"
            "1) above_id: 선 바로 위에서 관계 판단의 근거가 되는 글자(A 목록, A1 이 선에 가장 가까움). 없으면 NONE.\n"
            "2) below_id: 선 바로 아래의 근거 글자(B 목록, B1 이 선에 가장 가까움). 없으면 NONE.\n"
            "3) relation: 위 내용과 아래 내용의 관계를 하나 고르세요.\n"
            "- independent_sections: 서로 독립된 섹션(위 섹션이 끝나고 새 섹션이 시작)\n"
            "- heading_and_body: 위가 제목·소제목이고 아래가 그 설명·내용\n"
            "- same_paragraph_or_sentence: 같은 문단·문장이 이어짐\n"
            "- inside_table: 같은 표·항목-값 목록의 행 사이\n"
            "- button_and_description: 버튼·링크와 그 안내문이 연결됨\n"
            "- list_or_condition_continuation: 같은 목록·조건·각주가 이어짐\n"
            "- unknown: 판단할 수 없음\n"
            "좌표나 새 후보를 만들지 마세요. reason 에 근거를 짧게 쓰세요.\n"
            "위 글자(A, 선에서 가까운 순): " + json.dumps(above, ensure_ascii=False) +
            "\n아래 글자(B, 선에서 가까운 순): " + json.dumps(below, ensure_ascii=False))
    return {"text": text, "images": [crop, marked], "schema": schema, "crop_y": [top, bot]}


def accept_m2(parsed: dict | None, above: list[dict], below: list[dict]) -> tuple[bool, str]:
    """코드가 관계 유형으로 판정한다. 근거 ID 가 선에 가장 가까운 3개 밖이면 버린다."""
    if not isinstance(parsed, dict) or parsed.get("relation") not in RELATIONS:
        return False, "invalid_response"
    near_a = {a["id"] for a in above[:3]} | ({"NONE"} if not above else set())
    near_b = {b["id"] for b in below[:3]} | ({"NONE"} if not below else set())
    if parsed.get("above_id") not in near_a or parsed.get("below_id") not in near_b:
        return False, "evidence_mismatch"
    return parsed["relation"] == "independent_sections", parsed["relation"]
