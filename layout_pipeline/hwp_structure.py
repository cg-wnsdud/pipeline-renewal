"""HWP 구조 Region(hwp-structure/1, docs/20): 기존 parser-pipeline 의 HWP hybrid 경로를 이 파이프라인 Stage 1 에 연결한다.

기존 경로(nh_parser_fin ingest/loader.py + run.py, 코드는 hwp_legacy 에 그대로 복사):
1. 변환기(document-processor HTML → Chromium)가 화면에 배치한 DOM 행(표 밖 문단·표 행)의 문구와 CSS 좌표를 준다.
2. dom_rows_to_blocks 가 그 행을 렌더 페이지 좌표의 Region 으로 바꾼다(문단은 심의 항목 단위로 묶음, 표 행은 셀 구조).
3. 구조 행이 3개 이상이면 hybrid: 구조 Region 이 기본이고, 화면 검출(PaddleX) 블록 중 구조 Region 이 이미 담은 것은 버리고
   (_novel_visual_blocks) 표 행 안의 새 문구는 원문으로만 보탠다(_attach_visual_supplements). 남은 블록(그림 속 글자 등)만 시각 Region.
   구조 행이 3개 미만이면 기존처럼 시각 경로(이 파이프라인의 일반 경로)로 둔다.

이 파이프라인에서:
- 시각 블록 = Stage 1 레이아웃 검출 영역, 블록 문구 = 그 영역에 배정된 OCR 문구(기존 PaddleX 블록 content 와 같은 역할).
- 구조 Region 의 최종 문구는 DOM 행 문구(HWP 원문)이고 VLM 판독을 하지 않는다(read_result). 표 행은 셀 구조를 그대로 둔다.
- 기존 경로의 렌더 PDF 텍스트층·원본 구조 줄 보완(digital_lines·hwp_structure)은 옮기지 않았다: 표 행 안의 새 블록은 원문으로
  보태지 않고 시각 Region 으로 남겨 VLM 이 읽는다(_attach_visual_supplements 의 '렌더 글자 없음' 분기와 같음).
- 템플릿·상품 배정·라벨링은 이 파이프라인 의미 단계 그대로다.
"""
from __future__ import annotations

from .hwp_legacy import hybrid
from .hwp_legacy.hwp_html import dom_rows_to_blocks
from .jsonio import load
from .regions import Region

VERSION = "hwp-structure/1"
HYBRID_MIN_BLOCKS = 3          # 기존 loader.py: 구조 행 3개 이상이면 hybrid(HWP_VISUAL_AUGMENT 기본 on)
TABLE_READ = "hwp-dom-row/1"


def page_blocks(page) -> list[dict] | None:
    """HWP 페이지의 구조 블록(기존 dom_rows_to_blocks 결과). HWP 가 아니거나 DOM 행 기록이 없으면 None."""
    meta = (page.source.get("conversion") or {}).get("dom_rows") if page.source.get("kind") in ("hwp", "hwpx") else None
    if not meta or not meta.get("path"):
        return None
    css = meta.get("page_css_size") or [page.source["page_pt"][0] * 4 / 3, page.source["page_pt"][1] * 4 / 3]
    return dom_rows_to_blocks(load(meta["path"]), page_no=page.page_no, canvas=(page.width, page.height),
                              page_css_size=(float(css[0]), float(css[1])))


def _inside_text(bbox, items) -> str:
    """시각 블록 문구: 중심이 블록 안에 든 OCR 줄(위→아래). 기존 PaddleX 블록 content 처럼 겹친 다른 영역과 상관없이 담는다.
    엄격 배정(중심부를 한 영역만 포함)을 쓰면 구조 행 안의 작은 검출 상자 문구가 비어 중복 판정(_novel_visual_blocks)을 피한다."""
    x0, y0, x1, y1 = bbox
    inside = sorted((o.ocr_bbox[1], o.ocr_bbox[0], o.ocr_text) for o in items if o.ocr_text.strip()
                    and x0 <= (o.ocr_bbox[0] + o.ocr_bbox[2]) / 2 <= x1 and y0 <= (o.ocr_bbox[1] + o.ocr_bbox[3]) / 2 <= y1)
    return "\n".join(t for _, _, t in inside)


def apply(page, view_id: str, regions: list[Region], items: list) -> dict | None:
    """hybrid 이면 {regions: 구조 Region + 남은 시각 Region, meta: region_id → 구조 정보, summary}. 아니면 None."""
    blocks = page_blocks(page)
    if blocks is None or len(blocks) < HYBRID_MIN_BLOCKS:
        return None
    visual = [{"bbox": [int(v) for v in r.bbox], "content": _inside_text(r.bbox, items), "region": r} for r in regions]
    novel, suppressed = hybrid._novel_visual_blocks(visual, blocks)
    free, attached = hybrid._attach_visual_supplements(novel, blocks, None, None)
    out, meta = [], {}
    for i, b in enumerate(blocks):
        rid = f"{page.page_id}-h0-H{i:03d}"
        out.append(Region(rid, page.page_id, view_id, i, [float(v) for v in b["bbox"]], [float(v) for v in b["bbox"]],
                          b["kind"] == "table", f"hwp_{b['kind']}", None, 1.0, VERSION))
        meta[rid] = {"text": b["content"], "kind": b["kind"], "table": b.get("table"), "structured": b.get("structured"),
                     "visual_supplements": b.get("visual_supplements") or []}
    out += [v["region"] for v in free]
    return {"regions": out, "meta": meta,
            "summary": {"version": VERSION, "route": "hybrid", "structured_regions": len(blocks), "visual_regions": len(free),
                        "visual_suppressed": suppressed, "visual_attached": attached, "visual_total": len(regions)}}


def read_result(region: dict) -> dict:
    """구조 Region 의 판독 결과: DOM 행 문구 그대로(VLM 호출 없음). 표 행은 셀 구조(1행)."""
    h = region["hwp"]
    out = {"region_id": region["region_id"], "text": h["text"], "uncertain": False, "contract_ok": True, "contract_errors": [],
           "trace": {"calls": [], "kinds": ["hwp_structure"], "text_source": VERSION}}
    cells = ((h.get("table") or {}).get("cells")) or []
    if region["table_candidate"] and cells:
        out |= {"is_table": True, "n_rows": 1, "n_cols": len(cells), "rows": [[c["text"] for c in cells]],
                "cells": [{"row": 0, "col": c["col"], "rowspan": 1, "colspan": 1, "text": c["text"]} for c in cells]}
        out["trace"]["table_read"] = TABLE_READ
    return out
