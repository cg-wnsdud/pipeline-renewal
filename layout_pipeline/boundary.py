"""긴 이미지 자동 경계 실험(docs/boundary-experiment-plan.md). 결과는 candidate 계획으로만 저장한다.

탐색 OCR 은 문맥·보호 위치 확인용이며 최종 Region 이 아니다. 경계의 의미 안전은 이 모듈이 확정하지 않는다.
"""
from __future__ import annotations

import statistics

from .regions import parse_view
from .routing import InputView

SCOUT_ASPECT = 2.5          # 실험 정책값(서버 보장값 아님)
EDGE_PX = 2                 # 탐색 창 가장자리에 닿은 상자는 잘린 관측으로 제외
PAD_PX = 4                  # 보호 구간 위아래 여유
MIN_GAP_PX = 6              # 후보로 삼을 빈 띠 최소 높이
CONTEXT_PX = 300            # M2 입력 crop: 후보선 위아래 범위


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
