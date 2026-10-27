"""원시 검출 영역의 중첩·부분 포함·누락·타일 경계 진단. 진단은 영역을 고치지 않는다."""
from __future__ import annotations

from itertools import combinations

from .geometry import area, contains, intersection
from .regions import Region
from .routing import InputView

TILE_EDGE_PX = 2.0


def region_overlaps(regions: list[Region]) -> list[dict]:
    out = []
    for a, b in combinations(regions, 2):
        inter = intersection(a.bbox, b.bbox)
        if inter <= 0:
            continue
        union = area(a.bbox) + area(b.bbox) - inter
        if contains(a.bbox, b.bbox) or contains(b.bbox, a.bbox):
            relation = "contained"
        else:
            relation = "overlap"
        out.append({"a": a.region_id, "b": b.region_id, "relation": relation, "intersection_px": round(inter),
                    "iou": round(inter / union, 4), "frac_of_a": round(inter / area(a.bbox), 4),
                    "frac_of_b": round(inter / area(b.bbox), 4), "box": [max(a.bbox[0], b.bbox[0]),
                    max(a.bbox[1], b.bbox[1]), min(a.bbox[2], b.bbox[2]), min(a.bbox[3], b.bbox[3])]})
    return out


def region_text_relations(regions: list[Region], assignment: dict) -> dict[str, dict]:
    """영역별 배정 OCR 수와 경계에 걸친(부분 포함·모호) OCR 목록."""
    rel = {r.region_id: {"assigned": [], "crossing": [], "ambiguous": []} for r in regions}
    for a in assignment["assigned"]:
        rel[a["region_id"]]["assigned"].append(a["ocr_id"])
        review = a.get("assignment_review") or []
        if "low_ocr_overlap" in review or "ocr_bbox_crosses_region_boundary" in review:
            rel[a["region_id"]]["crossing"].append(a["ocr_id"])
        if "multiple_region_overlap" in review:
            rel[a["region_id"]]["ambiguous"].append(a["ocr_id"])
    for u in assignment["unassigned_texts"]:
        for c in u["candidates"]:
            key = "ambiguous" if u["reason"].startswith("ambiguous") else "crossing"
            if c["region_id"] in rel and (key == "ambiguous" or not c["core_inside"]):
                rel[c["region_id"]][key].append(u["ocr_id"])
    return rel


def tile_edge_regions(regions: list[Region], views: list[InputView], page_w: int, page_h: int) -> list[dict]:
    """확정 타일의 내부 경계에 닿은 영역. 잘린 영역일 수 있으므로 정상 채택하지 않고 표시한다."""
    out = []
    by_id = {v.view_id: v for v in views}
    for r in regions:
        v = by_id[r.view_id]
        if v.kind != "tile":
            continue
        x0, y0, x1, y1 = v.crop
        edges = []
        if y0 > 0 and r.bbox[1] - y0 <= TILE_EDGE_PX:
            edges.append("top")
        if y1 < page_h and y1 - r.bbox[3] <= TILE_EDGE_PX:
            edges.append("bottom")
        if x0 > 0 and r.bbox[0] - x0 <= TILE_EDGE_PX:
            edges.append("left")
        if x1 < page_w and x1 - r.bbox[2] <= TILE_EDGE_PX:
            edges.append("right")
        if edges:
            out.append({"region_id": r.region_id, "view_id": r.view_id, "edges": edges})
    return out


def server_block_comparison(regions: list[Region], blocks: list[dict]) -> dict:
    """서버 parsing_res_list 와 원시 layout_det_res 의 차이(비교 전용, Region 에 반영하지 않음)."""
    def best(box, others):
        scores = []
        for o in others:
            inter = intersection(box, o)
            if inter > 0:
                scores.append(inter / (area(box) + area(o) - inter))
        return max(scores, default=0.0)
    block_boxes = [b["bbox"] for b in blocks]
    region_boxes = [r.bbox for r in regions]
    return {"layout_det_boxes": len(regions), "parsing_res_blocks": len(blocks),
            "regions_without_matching_block": [r.region_id for r in regions if best(r.bbox, block_boxes) < 0.5],
            "blocks_without_matching_region": [i for i, b in enumerate(block_boxes) if best(b, region_boxes) < 0.5],
            "match_rule": "IoU >= 0.5"}
