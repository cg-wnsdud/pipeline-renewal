"""OCR 항목 → 레이아웃 영역 배정 또는 미배정.

배정 기준(core-containment/1):
  OCR 상자의 각 변을 짧은 변 × 0.25 만큼 안쪽으로 줄인 중심부(core)를 만든다.
  - core 를 완전히 포함하는 영역이 정확히 하나이고, core 와 겹치는 다른 영역이 없으면 assigned.
  - 둘 이상이 포함하면 ambiguous_multiple_regions, 하나가 포함하지만 다른 영역도 core 와 겹치면
    ambiguous_overlapping_regions, 포함 영역은 없고 core 와 겹침만 있으면 partial_overlap.
    core 는 어떤 영역과도 겹치지 않지만 원래 OCR bbox 는 영역과 겹치면 margin_overlap_only,
    원래 OCR bbox 도 겹치지 않으면 outside_layout. 면적이 0 이면 invalid_bbox.
  core_shrink=0 이면 엄격 포함(원래 OCR bbox 전체가 한 영역 안, 다른 영역과 겹침 없음)이다.
  중심점 하나로 판정하지 않는다. 0.25 는 배정 휴리스틱이며 잠정 상태다(status=provisional).
  0.2~0.4 에서 결과 수가 같다는 것은 정답 근거가 아니다. 완화 배정 전체를 원본과 대조한
  감사(docs/11)로 오배정 여부를 확인한다. 저장 bbox 는 어느 규칙에서도 바뀌지 않는다.

미배정 항목은 가까운 영역에 붙이거나 bbox 를 늘리지 않고 unassigned_texts 로 보존한다.

overlap-best/1(선택 규칙): 같은 view 에서 OCR bbox 와 양의 면적으로 겹치는 영역이 있으면 교집합/OCR 면적이
가장 큰 한 영역에 귀속한다. 동률이면 작은 영역·높은 점수·원시 순번 순이다. 약한 겹침·여러 영역 경합은
assignment_review 에 남긴다. 겹치는 영역이 없거나 OCR bbox 면적이 0 이면 미배정이다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .geometry import area, contains, core, intersection, protrusion
from .regions import OcrItem, Region

RULE_NAME = "core-containment/1"

REASONS = {
    "outside_layout": "원래 OCR bbox 가 어떤 레이아웃 영역과도 겹치지 않음",
    "margin_overlap_only": "원래 OCR bbox 는 영역과 겹치지만 중심부는 어떤 영역에도 닿지 않음",
    "partial_overlap": "레이아웃 영역에 일부만 포함됨(허용 오차를 넘는 돌출)",
    "ambiguous_multiple_regions": "2개 이상의 영역이 모두 포함함(원시 영역 중첩)",
    "ambiguous_overlapping_regions": "한 영역에 포함되지만 다른 영역과도 겹침",
    "invalid_bbox": "OCR bbox 면적이 0",
}


@dataclass(frozen=True)
class AssignmentRule:
    # 기본은 엄격 포함. 0.25 완화는 감사(work/audit/assignment-base)에서 완화 배정 177개 중 39개가 글자 획이
    # 영역 밖, 30개가 판단 불가로 확인되어 기본에서 제외했다. 비교용으로만 --core-shrink 0.25 를 쓴다.
    core_shrink: float = 0.0
    status: str = "baseline"
    overlap_best: bool = False   # overlap-best/1: 겹치는 OCR 줄은 가장 많이 겹친 단일 영역에 귀속

    @property
    def strict(self) -> bool:
        return self.core_shrink == 0 and not self.overlap_best

    def to_dict(self) -> dict:
        if self.overlap_best:
            return {"name": OVERLAP_RULE_NAME, "core_shrink": None, "strict": False, "status": self.status,
                    "statement": "OCR bbox 와 양의 면적으로 겹치는 같은 view 의 영역 중 OCR 면적 대비 겹침이 최대인 한 영역에 배정. "
                                 "동률이면 더 작은 영역, 더 높은 검출 점수, 원시 순번 순으로 선택. 겹치는 영역이 없을 때만 미배정."}
        return {"name": RULE_NAME, "core_shrink": self.core_shrink, "strict": self.strict, "status": self.status,
                "statement": "OCR 상자 중심부(각 변을 짧은 변×core_shrink 만큼 축소)를 정확히 한 영역이 포함하고 "
                             "다른 영역과 중심부가 겹치지 않으면 배정. 그 외는 미배정(사유 기록)."}


OVERLAP_RULE_NAME = "overlap-best/1"
OVERLAP_RULE = AssignmentRule(status="provisional", overlap_best=True)


def classify(item: OcrItem, regions: list[Region], rule: AssignmentRule) -> tuple[str, list[dict]]:
    b = item.ocr_bbox
    if area(b) <= 0:
        return "invalid_bbox", []
    c = core(b, rule.core_shrink)
    candidates = []
    for r in regions:
        inter = intersection(b, r.bbox)
        if inter <= 0:
            continue
        candidates.append({"region_id": r.region_id, "inside_ratio": round(inter / area(b), 4),
                           "intersection_px": inter,
                           "exact_inside": contains(r.bbox, b), "protrusion_px": round(protrusion(b, r.bbox), 2),
                           "core_inside": contains(r.bbox, c), "core_touch": intersection(c, r.bbox) > 0})
    if rule.overlap_best:
        return ("assigned" if candidates else "outside_layout"), candidates
    full = [x for x in candidates if x["core_inside"]]
    touch = [x for x in candidates if x["core_touch"]]
    if len(full) >= 2:
        status = "ambiguous_multiple_regions"
    elif len(full) == 1:
        status = "assigned" if len(touch) == 1 else "ambiguous_overlapping_regions"
    elif touch:
        status = "partial_overlap"
    else:
        status = "margin_overlap_only" if candidates else "outside_layout"
    return status, candidates


class PartitionError(AssertionError):
    pass


def assign(items: list[OcrItem], regions: list[Region], rule: AssignmentRule) -> dict:
    """view 단위로 배정한다(타일 응답 사이의 교차 배정 없음)."""
    by_view: dict[str, list[Region]] = {}
    for r in regions:
        by_view.setdefault(r.view_id, []).append(r)
    bbox_of = {r.region_id: r.bbox for r in regions}
    region_of = {r.region_id: r for r in regions}
    assigned, unassigned = [], []
    for item in items:
        status, candidates = classify(item, by_view.get(item.view_id, []), rule)
        if status == "assigned":
            if rule.overlap_best:
                hit = max(candidates, key=lambda x: (x["intersection_px"], -area(bbox_of[x["region_id"]]),
                                                     region_of[x["region_id"]].raw_score,
                                                     -region_of[x["region_id"]].source_index))
            else:
                hit = next(x for x in candidates if x["core_inside"])
            review = []
            if rule.overlap_best:
                if hit["inside_ratio"] < 0.5:
                    review.append("low_ocr_overlap")
                if len(candidates) > 1:
                    review.append("multiple_region_overlap")
                if not hit["exact_inside"]:
                    review.append("ocr_bbox_crosses_region_boundary")
            assigned.append({"ocr_id": item.ocr_id, "region_id": hit["region_id"], "page_id": item.page_id,
                             "view_id": item.view_id, "ocr_text": item.ocr_text, "ocr_bbox": item.ocr_bbox,
                             "score": item.score, "empty_text": item.empty, "exact_inside": hit["exact_inside"],
                             "protrusion_px": hit["protrusion_px"], "inside_ratio": hit["inside_ratio"]}
                            | ({"assignment_review": review, "candidate_region_ids": [c["region_id"] for c in candidates]}
                               if rule.overlap_best else {}))
        else:
            unassigned.append({"ocr_id": item.ocr_id, "source": item.source, "source_index": item.source_index,
                               "page_id": item.page_id, "view_id": item.view_id, "ocr_text": item.ocr_text,
                               "ocr_bbox": item.ocr_bbox, "score": item.score, "reason": status,
                               "reason_text": REASONS[status], "candidates": candidates, "empty_text": item.empty,
                               "review_required": True, "final": False})
    checks = verify_partition(items, regions, assigned, unassigned)
    return {"rule": rule.to_dict(), "assigned": assigned, "unassigned_texts": unassigned, "checks": checks}


def verify_partition(items: list[OcrItem], regions: list[Region], assigned: list[dict], unassigned: list[dict]) -> dict:
    """모든 OCR 항목이 배정/미배정 중 정확히 한 곳에 한 번 나타나는지 검사한다. 실패하면 예외."""
    ids = [i.ocr_id for i in items]
    seen = Counter([a["ocr_id"] for a in assigned] + [u["ocr_id"] for u in unassigned])
    region_ids = {r.region_id for r in regions}
    missing = [i for i in ids if seen[i] == 0]
    duplicated = sorted(k for k, v in seen.items() if v > 1)
    unknown = sorted(set(seen) - set(ids))
    dangling = sorted({a["region_id"] for a in assigned} - region_ids)
    checks = {"ocr_items": len(ids), "assigned": len(assigned), "unassigned": len(unassigned),
              "missing": missing, "duplicated": duplicated, "unknown": unknown, "dangling_region_refs": dangling,
              "unique_ocr_ids": len(set(ids)) == len(ids)}
    checks["passed"] = (not missing and not duplicated and not unknown and not dangling and checks["unique_ocr_ids"]
                        and len(assigned) + len(unassigned) == len(ids))
    if not checks["passed"]:
        raise PartitionError(f"OCR 배정 분할 검증 실패: {checks}")
    return checks
