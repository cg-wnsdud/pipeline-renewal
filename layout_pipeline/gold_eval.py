"""정답(new-pipeline 의 검토된 줄 정답) 대비 평가. 서로 다른 두 지표를 섞지 않는다.

1. geometry (exact-containment/1, 고정): 정답 줄 bbox 가 원시 레이아웃 bbox 에 허용 오차 없이
   완전히 들어가는 영역 수로 none / partial / single / multiple 을 정한다. 배정 규칙·허용 오차와 무관하며
   모든 검출 설정 비교에서 같은 기준을 쓴다. 정답 bbox 자체가 OCR 상자 여백을 포함할 수 있으므로
   partial 은 포함 면적비 구간과 함께 보고한다(재분류하지 않음).
2. assignment_policy: 배정 규칙(classify)을 정답 줄에 적용한 단일 영역 배정률. 검출 재현율이 아니다.

정답 줄은 보조 근거다. 좌표계(렌더 해시)가 다르면 평가하지 않는다. 최종 문구 정확도는 여기서 다루지 않는다.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .assignment import AssignmentRule, classify
from .geometry import area, contains, intersection
from .jsonio import load
from .regions import OcrItem, Region

GOLD_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "gold"
GEOMETRY_CRITERION = {"name": "exact-containment/1",
                      "statement": "정답 줄 bbox 전체가 원시 레이아웃 bbox 안(허용 오차 없음). 설정 비교 내내 고정."}
TABLE_CRITERION = {"name": "table-majority/1",
                   "statement": "정답 표 줄 bbox 면적의 절반 이상이 같은 표 후보 안에 있으면 그 후보에 속한 것으로 센다."}


def load_gold(page_id: str, page_sha: str, size: list[int]) -> tuple[dict | None, str | None]:
    path = GOLD_DIR / f"{page_id}.json"
    if not path.is_file():
        return None, None
    gold = load(path)
    ref_sha = load(GOLD_DIR / "page-sha.json").get(page_id)
    if gold["ref_size"] != size or ref_sha != page_sha:
        return None, f"정답 좌표계 불일치(size {gold['ref_size']} vs {size}, sha 일치={ref_sha == page_sha})"
    return gold, None


def geometry_status(bbox: list[float], regions: list[Region]) -> tuple[str, list[str], float]:
    full = [r.region_id for r in regions if contains(r.bbox, bbox)]
    a = area(bbox)
    best = max((intersection(bbox, r.bbox) / a for r in regions), default=0.0) if a > 0 else 0.0
    if len(full) >= 2:
        return "multiple", full, best
    if len(full) == 1:
        return "single", full, best
    return ("partial" if best > 0 else "none"), full, best


def _bucket(ratio: float) -> str:
    return "ge_0.95" if ratio >= 0.95 else ("0.5_to_0.95" if ratio >= 0.5 else "lt_0.5")


def evaluate_page(page_id: str, page_sha: str, size: list[int], regions: list[Region], rule: AssignmentRule) -> dict | None:
    gold, problem = load_gold(page_id, page_sha, size)
    if problem:
        return {"evaluated": False, "reason": problem}
    if gold is None:
        return None
    lines = []
    geo: Counter = Counter()
    partial_hist: Counter = Counter()
    policy: Counter = Counter()
    for line in gold["lines"]:
        bbox = [float(v) for v in line["bbox"]]
        g_status, g_regions, best = geometry_status(bbox, regions)
        item = OcrItem(line["id"], page_id, "gold", 0, line["text"], bbox, [], 1.0, source="gold")
        p_status, candidates = classify(item, regions, rule)
        p_status = "covered" if p_status == "assigned" else p_status
        if not line.get("optional"):
            geo[g_status] += 1
            policy[p_status] += 1
            if g_status == "partial":
                partial_hist[_bucket(best)] += 1
        lines.append({"gold_id": line["id"], "text": line["text"], "bbox": line["bbox"],
                      "optional": bool(line.get("optional")), "geometry": g_status, "geometry_regions": g_regions,
                      "best_inside_ratio": round(best, 4), "status": p_status,
                      "candidates": [c["region_id"] for c in candidates]})
    required = sum(1 for x in lines if not x["optional"])
    return {
        "evaluated": True, "source": path_name(page_id), "required_lines": required,
        "geometry": {"criterion": GEOMETRY_CRITERION, "counts": dict(geo), "partial_inside_ratio": dict(partial_hist)},
        "assignment_policy": {"rule": rule.to_dict(), "counts": dict(policy),
                              "single_rate": round(policy["covered"] / required, 4) if required else None},
        # 하위 호환: 보고서/뷰어가 쓰는 배정 정책 기준 키
        "counts": dict(policy), "covered_ratio": round(policy["covered"] / required, 4) if required else None,
        "lines": lines,
        "uncovered": [x for x in lines if x["status"] != "covered" and not x["optional"]],
        "tables": _tables(page_id, gold, regions),
    }


def path_name(page_id: str) -> str:
    return f"{page_id}.json"


def _tables(page_id: str, gold: dict, regions: list[Region]) -> dict:
    """gold-relations.json 의 표 목록만 평가한다(모든 표를 망라한 목록이 아님)."""
    rel = load(GOLD_DIR.parent / "gold-relations.json")
    by_id = {line["id"]: line for line in gold["lines"]}
    candidates = [r for r in regions if r.table_candidate]
    tables = []
    touched: set[str] = set()
    for table in rel["tables"]:
        if table["page"] != page_id:
            continue
        ids = [f"{page_id}-{g}" for row in table["rows"] for cell in row for g in cell]
        boxes = [[float(v) for v in by_id[i]["bbox"]] for i in ids if i in by_id]
        hits: Counter = Counter()
        for b in boxes:
            for r in candidates:
                if area(b) > 0 and intersection(b, r.bbox) / area(b) >= 0.5:
                    hits[r.region_id] += 1
        best_id, best_n = hits.most_common(1)[0] if hits else (None, 0)
        touched.update(hits)
        frac = best_n / len(boxes) if boxes else 0.0
        state = "single_candidate" if frac >= 0.9 else ("partial_or_split" if hits else "missed")
        tables.append({"gold_table": table["id"], "lines": len(boxes), "best_candidate": best_id,
                       "best_fraction": round(frac, 4), "candidates_hit": dict(hits), "status": state})
    return {"criterion": TABLE_CRITERION, "gold_tables": tables,
            "candidates_not_in_gold_tables": [r.region_id for r in candidates if r.region_id not in touched],
            "note": "gold-relations.json 의 표 목록은 상품 관계 평가용이며 모든 표를 망라하지 않는다."}
