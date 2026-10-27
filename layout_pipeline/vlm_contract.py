"""후속 VLM 처리 계약(인터페이스 정의만, 이 단계에서는 호출하지 않는다).

- 일반 영역: 영역 안의 최종 문구를 판독한다(read_text).
- OCR 표 후보(table_candidate=True)만: 표 여부와 내용을 같은 호출로 확인한다(table_check).
  is_table=false 여도 같은 bbox 의 일반 영역으로 남고 문구를 유지한다. 같은 표를 두 번 전사하지 않는다.
- 표 후보가 아닌 영역에 표 확인·전역 표 재구성 요청을 만들지 않는다.
- VLM 이미지는 원래 배치의 페이지·타일 또는 검출 영역 전체다. 줄 시트(OCR 줄 crop 재배치)는 금지.
- VLM 은 새 bbox/새 Region 을 만들지 않는다. 응답은 요청한 region_id 집합과 정확히 같아야 한다.
- 긴 이미지 경계 확인은 별도 작업(boundary-plan/1)이며 표 확인과 섞지 않는다.
- unassigned_texts 는 VLM 영역 요청에 넣지 않는다. OCR 출처의 검토 대상으로 그대로 전달한다.
"""
from __future__ import annotations

import math

VLM_IMAGE_PIXEL_BUDGET = 2_580_480   # VLM 이미지 처리기 예산(기존 서버 프로파일). VLM 입력 검사에만 쓴다.
IMAGE_SCOPES = ("page", "tile", "region_crop")
TASKS = ("read_text", "table_check")

READ_RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["region_id", "text", "uncertain"],
    "properties": {"region_id": {"type": "string"}, "text": {"type": ["string", "null"]},
                   "uncertain": {"type": "boolean"}},
}
TABLE_RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["region_id", "is_table", "text", "uncertain"],
    "properties": {"region_id": {"type": "string"}, "is_table": {"type": "boolean"},
                   "text": {"type": ["string", "null"]}, "uncertain": {"type": "boolean"}},
}


class ContractError(ValueError):
    pass


def region_crop_box(bbox: list[float], width: int, height: int) -> list[int]:
    """검출 영역 전체 crop 의 정수 범위(floor/ceil). 저장 bbox 는 바꾸지 않는다."""
    return [max(0, math.floor(bbox[0])), max(0, math.floor(bbox[1])),
            min(width, math.ceil(bbox[2])), min(height, math.ceil(bbox[3]))]


def plan_requests(page_record: dict, read_scope: str | None = None) -> list[dict]:
    """페이지의 Region 마다 정확히 한 개의 요청을 계획한다. read_scope 는 R3 비교 전 미정(None)."""
    page = page_record["page"]
    views = {v["view_id"]: v for v in page_record["route"]["views"]}
    drafts: dict[str, list[dict]] = {}
    for a in page_record["assignments"]:
        drafts.setdefault(a["region_id"], []).append({"ocr_id": a["ocr_id"], "ocr_text": a["ocr_text"],
                                                       "ocr_bbox": a["ocr_bbox"]})
    out = []
    for r in page_record["regions"]:
        task = "table_check" if r["table_candidate"] else "read_text"
        scope = "region_crop" if task == "table_check" else read_scope
        if scope == "region_crop":
            crop = region_crop_box(r["bbox"], page["width"], page["height"])
        elif scope in ("page", "tile"):
            crop = views[r["view_id"]]["crop"] if scope == "tile" else [0, 0, page["width"], page["height"]]
        else:
            crop = None
        pixels = (crop[2] - crop[0]) * (crop[3] - crop[1]) if crop else None
        out.append({"task": task, "region_id": r["region_id"], "page_id": r["page_id"], "view_id": r["view_id"],
                    "bbox": r["bbox"], "image_scope": scope, "image_crop": crop, "image_pixels": pixels,
                    "exceeds_vlm_pixel_budget": None if pixels is None else pixels > VLM_IMAGE_PIXEL_BUDGET,
                    "ocr_draft": drafts.get(r["region_id"], []),
                    "result_schema": "TABLE_RESULT_SCHEMA" if task == "table_check" else "READ_RESULT_SCHEMA"})
    return out


def validate_requests(requests: list[dict], regions: list[dict]) -> dict:
    by_id = {r["region_id"]: r for r in regions}
    ids = [q["region_id"] for q in requests]
    problems = []
    if sorted(ids) != sorted(by_id):
        problems.append("요청 region_id 집합이 Region 집합과 다름(누락 또는 중복)")
    for q in requests:
        r = by_id.get(q["region_id"])
        if r is None:
            continue
        if q["task"] not in TASKS:
            problems.append(f"{q['region_id']}: 알 수 없는 작업 {q['task']}")
        if q["task"] == "table_check" and not r["table_candidate"]:
            problems.append(f"{q['region_id']}: 표 후보가 아닌 영역에 table_check")
        if r["table_candidate"] and q["task"] != "table_check":
            problems.append(f"{q['region_id']}: 표 후보가 table_check 가 아님")
        if q["image_scope"] not in IMAGE_SCOPES + (None,):
            problems.append(f"{q['region_id']}: 허용되지 않는 이미지 범위 {q['image_scope']}")
        if q["bbox"] != r["bbox"]:
            problems.append(f"{q['region_id']}: bbox 가 Region 과 다름")
    if problems:
        raise ContractError("; ".join(problems))
    return {"requests": len(requests), "read_text": sum(q["task"] == "read_text" for q in requests),
            "table_check": sum(q["task"] == "table_check" for q in requests), "passed": True}


def merge_results(regions: list[dict], results: list[dict], reviews: dict[str, dict]) -> list[dict]:
    """VLM 결과를 Region 에 붙인다. bbox·ID 는 그대로, 거짓 표도 같은 영역·문구로 남긴다.

    - text=None 은 판독 실패, "" 은 글자 없음으로 판독. 둘을 섞지 않는다.
    - is_table 은 실제 boolean 이어야 한다(문자열을 bool() 로 바꾸지 않는다).
    - needs_review 는 reviews(vlm_tasks.review_reasons: OCR/VLM 불일치·판독 실패·계약 오류·중첩 등)로 정한다.
      uncertain=false 만으로 검토 불필요를 결정하지 않는다."""
    by_id = {x["region_id"]: x for x in results}
    if sorted(by_id) != sorted(r["region_id"] for r in regions) or len(by_id) != len(results):
        raise ContractError("VLM 결과의 region_id 집합이 요청과 다름")
    out = []
    for r in regions:
        res = by_id[r["region_id"]]
        text = res.get("text")
        if text is not None and not isinstance(text, str):
            raise ContractError(f"{r['region_id']}: text 타입 {type(text).__name__}")
        if r["table_candidate"]:
            if not isinstance(res.get("is_table"), bool):
                raise ContractError(f"{r['region_id']}: is_table 이 boolean 이 아님({res.get('is_table')!r})")
            verified = res["is_table"]
        else:
            if "is_table" in res:
                raise ContractError(f"{r['region_id']}: 표 후보가 아닌 영역에 표 판정이 있음")
            verified = None
        review = reviews.get(r["region_id"])
        if review is None:
            raise ContractError(f"{r['region_id']}: 검토 근거(review_reasons) 없음")
        out.append({"region_id": r["region_id"], "page_id": r["page_id"], "bbox": r["bbox"], "text": text,
                    "text_status": "read_failed" if text is None else ("no_text" if text == "" else "read"),
                    "table_verified": verified, "needs_review": review["needs_review"],
                    "review_reasons": review["quality_reasons"] + review["context_reasons"]})
    return out
