"""최종 결과(layout-result/1) 구성과 계약 검사.

사용자 결과에는 원본 페이지·좌표계, Region(원본 bbox·VLM 최종 문구·판독 상태·표 확인과 행 관계·검토 근거),
별도 미배정 OCR, 미처리 페이지·실패 상태만 둔다. 요청·원시 응답·OCR 초안·평가 근거는 trace 로 분리한다.
계약 통과는 구조·보존 검사이며 내용 품질 통과가 아니다(quality 는 항상 not_verified, 평가는 별도 실행).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .page_read import BOUNDARY_REF_MIN_INSIDE
from .read_stage import integrate_region
from .text_eval import norm

CONTRACT = "layout-result/1"
TEXT_STATUSES = {"read", "no_text", "read_failed", "not_read"}
TABLE_STATUSES = {"table", "not_table", "unknown"}
# 결과의 미배정 분류(unassigned-output/1). Stage 1 배정(엄격 포함)과 VLM 입력은 바꾸지 않는다. 엄격 배정에서 미배정인 OCR 줄도
# 면적의 이 비율 이상이 한 영역 안이면 그 영역 안의 글자(VLM 이 그 영역 문구로 읽는 대상)로 보고 결과의 미배정 목록에 넣지 않는다.
# 값은 VLM 지시('경계에 걸친 줄은 대부분이 상자 안이면 그 영역에 쓴다')·재판독/품질 검토의 OCR 근거(page_read.region_ocr)와 같다.
IN_REGION_MIN_INSIDE = BOUNDARY_REF_MIN_INSIDE
UNASSIGNED_OUTPUT_RULE = "unassigned-output/1"


def in_region_owner(u: dict) -> str | None:
    """엄격 배정의 미배정 OCR 줄이 면적 IN_REGION_MIN_INSIDE 이상 들어간 영역(가장 많이 들어간 하나). 없으면 None."""
    best = max(u.get("candidates") or [], key=lambda c: c["inside_ratio"], default=None)
    return best["region_id"] if best and best["inside_ratio"] >= IN_REGION_MIN_INSIDE else None


def output_unassigned(rec: dict) -> list[dict]:
    """결과에 남길 미배정 OCR 줄: 어느 영역에도 면적 IN_REGION_MIN_INSIDE 이상 들어가지 않은 줄(영역 밖·거의 걸치지 않은 줄)."""
    return [u for u in rec["unassigned_texts"] if in_region_owner(u) is None]


def build_result(records: list[dict], reads: dict[str, dict], meta: dict) -> dict:
    pages, regions, unassigned, issues = [], [], [], []
    for rec in records:
        page = rec["page"]
        page_regions = [integrate_region(rec, r, reads.get(r["region_id"])) for r in rec["regions"]]
        if rec["status"] == "CANDIDATE_RUN":
            for r in page_regions:
                # 미검증 후보 경계 타일에서 나온 영역: 확정 결과가 아님을 영역마다 남긴다.
                r["candidate_boundary"] = True
                r["needs_review"] = True
                r["review"]["context"].append("candidate_boundary_unvalidated")
        mark_overlap_duplicates(page_regions, rec["diagnostics"]["region_overlaps"])
        regions += page_regions
        for u in output_unassigned(rec):
            unassigned.append({"ocr_id": u["ocr_id"], "page_id": u["page_id"], "ocr_text": u["ocr_text"],
                               "ocr_bbox": u["ocr_bbox"], "score": u["score"], "reason": u["reason"],
                               "candidate_region_ids": [c["region_id"] for c in u["candidates"]],
                               "empty_text": u["empty_text"], "source": u["source"], "final": False,
                               "review_required": True})
        for i in rec["issues"]:
            issues.append({"page_id": page["page_id"], "code": i["code"], "severity": i["severity"], "message": i["message"]})
        for r in page_regions:
            if r["text_status"] in ("read_failed", "not_read"):
                issues.append({"page_id": page["page_id"], "code": "REGION_READ_FAILED", "severity": "error",
                               "message": f"{r['region_id']}: {r['text_status']} {r.get('contract_errors') or ''}".strip()})
            if r["table"] and r["table"]["status"] == "unknown":
                issues.append({"page_id": page["page_id"], "code": "TABLE_CHECK_UNKNOWN", "severity": "error",
                               "message": f"{r['region_id']}: 표 확인 결과 없음(is_table 미정, false 로 간주하지 않음)"})
        status = Counter(r["text_status"] for r in page_regions)
        pages.append({
            "page_id": page["page_id"], "submission_id": page["submission_id"], "file_id": page["file_id"],
            "page_no": page["page_no"], "width": page["width"], "height": page["height"],
            "coordinate_system": "page_px_xyxy", "image_sha256": page["image_sha256"],
            "source_name": page["source"].get("source_name"), "route": rec["route"]["route"],
            "execution_status": rec["status"],
            "quality_status": {"READY": "not_verified", "CANDIDATE_RUN": "candidate_unvalidated"}.get(rec["status"], "not_processed"),
            "boundary": None if rec["route"]["route"] != "long" else {
                "plan_status": (rec["route"].get("boundary_plan") or {}).get("status"),
                "plan_version": (rec["route"].get("boundary_plan") or {}).get("plan_version"),
                "cuts": (rec["route"].get("boundary_plan") or {}).get("cuts"),
                "tiles": [{"view_id": v["view_id"], "crop": v["crop"], "aspect": v["aspect"]} for v in rec["route"]["views"]],
                "validated": False if rec["status"] != "READY" else True},
            "reading": None if rec["status"] not in ("READY", "CANDIDATE_RUN") else {
                "regions": len(page_regions), **{k: status.get(k, 0) for k in sorted(TEXT_STATUSES)},
                "needs_review": sum(r["needs_review"] for r in page_regions),
                "needs_review_quality": sum(bool(r["review"]["quality"]) for r in page_regions),
                "tables": dict(Counter(r["table"]["status"] for r in page_regions if r["table"]))},
            "unassigned_texts": len(output_unassigned(rec)),
            "ocr_in_region_by_overlap": len(rec["unassigned_texts"]) - len(output_unassigned(rec)),
        })
    result = {
        "contract": CONTRACT, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "regions[].text 는 VLM 최종 문구다. unassigned_texts 는 어느 레이아웃 영역에도 면적 "
                f"{IN_REGION_MIN_INSIDE:.0%} 이상 들어가지 않은 OCR 원문({UNASSIGNED_OUTPUT_RULE})이며 최종 문구와 합치지 않는다. "
                "판독 실패 영역(text=null)의 ocr_reference 도 OCR 원문(final=false)이다. "
                "quality_status=not_verified: 내용 품질은 별도 평가 대상이다.",
        "settings": meta | {"unassigned_output": {"name": UNASSIGNED_OUTPUT_RULE, "in_region_min_inside": IN_REGION_MIN_INSIDE}},
        "pages": pages,
        "regions": [{k: v for k, v in r.items() if k not in ("contract_errors", "uncertain", "trace")}
                    | {"trace_calls": (r.get("trace") or {}).get("calls")} for r in regions],
        "unassigned_texts": unassigned, "issues": issues,
    }
    result["contract_checks"] = check_result(result, records)
    return result, regions


def mark_overlap_duplicates(page_regions: list[dict], overlaps: list[dict]) -> None:
    """원시 bbox 가 겹치는 두 Region 의 출력에 같은 문구가 함께 있으면 맥락 검토 근거로 남긴다.

    한 영역의 줄(정규화 4자 이상)이 겹친 상대 영역 문구 안에 그대로 들어 있으면 같은 위치를 두 영역이 함께 읽었을 수 있다
    (예: '고향사랑금리 0.05%p…' 와 '특별금리 고향사랑금리 0.05%p…'). 영역·문구는 지우거나 고치지 않는다.
    겹치지 않는 영역의 같은 문구(실제 반복)는 표시하지 않는다."""
    by_id = {r["region_id"]: r for r in page_regions}

    def mark(rid: str, line: str, other: str) -> None:
        r = by_id[rid]
        if "overlap_duplicate_text" not in r["review"]["context"]:
            r["review"]["context"].append("overlap_duplicate_text")
        lines = r["review"].setdefault("measures", {}).setdefault("overlap_duplicate_lines", [])
        if {"line": line, "also_in": other} not in lines:
            lines.append({"line": line, "also_in": other})
        r["needs_review"] = True

    for o in overlaps:
        a, b = by_id.get(o["a"]), by_id.get(o["b"])
        if not a or not b or not a["text"] or not b["text"]:
            continue
        for x, y in ((a, b), (b, a)):
            other = norm(y["text"])
            for line in x["text"].splitlines():
                n = norm(line)
                if len(n) >= 4 and n in other:
                    mark(x["region_id"], line.strip(), y["region_id"])
                    mark(y["region_id"], line.strip(), x["region_id"])


def check_result(result: dict, records: list[dict]) -> dict:
    """구조·보존 검사. 실패해도 결과를 지우지 않고 항목을 남긴다."""
    problems = []
    expected = {r["region_id"]: r for rec in records for r in rec["regions"]}
    got = [r["region_id"] for r in result["regions"]]
    if sorted(got) != sorted(expected) or len(set(got)) != len(got):
        problems.append("Region 집합이 Stage 1 과 다름(누락·중복)")
    for r in result["regions"]:
        src = expected.get(r["region_id"])
        if src and r["bbox"] != src["bbox"]:
            problems.append(f"{r['region_id']}: bbox 변경")
        if r["text_status"] not in TEXT_STATUSES:
            problems.append(f"{r['region_id']}: text_status {r['text_status']}")
        if (r["text"] is None) != (r["text_status"] in ("read_failed", "not_read")):
            problems.append(f"{r['region_id']}: text 와 text_status 불일치")
        if r["text"] is not None and not isinstance(r["text"], str):
            problems.append(f"{r['region_id']}: text 타입")
        if r["table_candidate"]:
            if not r["table"] or r["table"]["status"] not in TABLE_STATUSES:
                problems.append(f"{r['region_id']}: 표 후보의 table 상태 없음")
        elif r["table"] is not None:
            problems.append(f"{r['region_id']}: 표 후보가 아닌데 표 결과가 있음")
    n_un = sum(len(output_unassigned(rec)) for rec in records)
    if len(result["unassigned_texts"]) != n_un:
        problems.append("미배정 OCR 수가 Stage 1 미배정 중 영역 밖 줄 수와 다름")
    ocr_total = sum(rec["ocr_items_total"] for rec in records)
    ocr_split = sum(len(rec["assignments"]) + len(rec["unassigned_texts"]) for rec in records)
    if ocr_total != ocr_split:
        problems.append("원시 OCR 분할 불일치")
    return {"passed": not problems, "problems": problems, "regions": len(got), "unassigned_texts": n_un,
            "ocr_items": ocr_total,
            "statement": "구조·보존 계약 검사. 원문·위치·표 관계 품질은 검사하지 않는다."}
