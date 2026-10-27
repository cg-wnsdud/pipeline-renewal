"""구조·관계 평가(평가 전용). 문구 보존(text_eval)과 따로 센다.

- 연결 단위(fixtures/structure-gold.json units): 제목-설명·조건-값·도해 경로처럼 이 순서로 붙어 나와야 하는 줄 묶음.
  보존 = 단위 줄을 이어 붙인 문자열이 출력 어느 부분과 편집거리 10% 이하.
  실패는 '줄 누락'(단위의 어떤 줄이 출력에 없음)과 '순서·블록 혼합'(줄은 모두 있으나 붙어 있지 않음)으로 나눈다.
- 표 행 대응(table_rows): 한 출력 행에 함께 있어야 하는 셀 묶음.
- OCR 대비 결과: 정답 줄마다 OCR(같은 위치) 정오와 VLM(소속 영역 문구) 정오를 함께 본다.
- 검토 표시 정확도: 영역별 실제 오류 유무와 품질 근거 표시 유무의 교차표.
"""
from __future__ import annotations

import re
from collections import Counter

from .geometry import area, intersection
from .text_eval import membership, norm, semiglobal, tolerance
from .vlm_tasks import row_cells

CONTENT_RE = re.compile(r"[^0-9A-Za-z%가-힣ㄱ-ㆎ]")


def _found(p: str, out: str) -> bool:
    return bool(p) and bool(out) and semiglobal(p, out) <= tolerance(p)


def evaluate_units(text: str | None, units: list[list[str]], gold_by_id: dict[str, dict]) -> dict:
    out = norm(text or "")
    rows = []
    for unit in units:
        texts = [gold_by_id[g]["text"] for g in unit if g in gold_by_id]
        joined = norm("".join(texts))
        if _found(joined, out):
            status = "kept"
        elif all(_found(norm(t), out) for t in texts):
            status = "order_or_block_mixed"
        else:
            status = "lines_missing"
        rows.append({"unit": unit, "texts": texts, "status": status})
    c = Counter(r["status"] for r in rows)
    return {"units": len(rows), "kept": c.get("kept", 0), "order_or_block_mixed": c.get("order_or_block_mixed", 0),
            "lines_missing": c.get("lines_missing", 0), "detail": rows}


def evaluate_table_rows(rows_out, gold_rows: list[list[str]], gold_by_id: dict[str, dict]) -> dict:
    out_rows = [norm(" ".join(row_cells(r))) for r in (rows_out or [])]
    detail = []
    for gr in gold_rows:
        cells = [norm(gold_by_id[g]["text"]) for g in gr if g in gold_by_id]
        ok = any(all(_found(c, r) for c in cells) for r in out_rows)
        detail.append({"row": gr, "kept": ok})
    return {"rows": len(detail), "kept": sum(d["kept"] for d in detail), "output_rows": len(out_rows), "detail": detail}


def ocr_text_for(gold_bbox, ocr_items: list[dict]) -> str | None:
    best, best_iou = None, 0.0
    for o in ocr_items:
        b = o["ocr_bbox"]
        inter = intersection(gold_bbox, b)
        if inter <= 0:
            continue
        iou = inter / (area(gold_bbox) + area(b) - inter)
        if iou > best_iou:
            best, best_iou = o, iou
    return best["ocr_text"] if best is not None and best_iou >= 0.5 else None


def content(text: str) -> str:
    """글자·숫자·% 만 남긴 비교 키(글머리 기호·문장부호·띄어쓰기 차이를 뺀 내용 수준)."""
    return CONTENT_RE.sub("", norm(text))


def _category(ocr: str | None, gold: str, home_text: str, key) -> str:
    g, h = key(gold), key(home_text)
    if not g:
        return "symbol_only_line"
    vlm_ok = bool(g) and g in h
    if ocr is None:
        return "no_ocr_" + ("vlm_ok" if vlm_ok else "vlm_wrong")
    o = key(ocr)
    if o == g:
        return "both_correct" if vlm_ok else "vlm_broke_correct_ocr"
    if vlm_ok:
        return "vlm_corrected_ocr"
    return "vlm_followed_ocr_error" if o and o in h else "both_wrong_differently"


def ocr_vlm_outcomes(page_eval_lines: list[dict], gold_by_id: dict[str, dict], ocr_items: list[dict],
                     regions: list[dict]) -> list[dict]:
    """정답 줄(소속 영역이 있는 줄)마다 같은 위치 OCR(IoU≥0.5) 정오와 소속 영역 VLM 문구 정오를 비교한다.

    두 수준으로 센다. strict = 정규화(공백·'|' 제거, 기호 통일) 후 일치, content = 글자·숫자·% 만 비교.
    VLM 이 OCR 오답을 따라감 = VLM 이 틀림 + 소속 영역 문구 안에 OCR 오답 원문이 그대로 있음.
    다른 영역에 같은 문구가 있어도 맞힌 것으로 보지 않는다."""
    rows = []
    for g in page_eval_lines:
        if g["geometry"] != "home":
            continue
        gold = gold_by_id[g["gold_id"]]
        bbox = [float(v) for v in gold["bbox"]]
        ocr = ocr_text_for(bbox, ocr_items)
        home = next(r for r in regions if membership(bbox, r["bbox"]) == "in")
        text = home.get("text") or ""
        rows.append({"gold_id": g["gold_id"], "gold": gold["text"], "ocr": ocr, "region_id": home["region_id"],
                     "strict": _category(ocr, gold["text"], text, norm), "content": _category(ocr, gold["text"], text, content)})
    return rows


def region_has_error(e: dict, unit_eval: dict | None, row_eval: dict | None, text_status: str, text: str | None) -> list[str]:
    """평가상 실제 오류 근거. 근사 줄은 글자·숫자 수준(content)에서도 다를 때만 글자 오류로 센다."""
    errs = []
    if text_status in ("read_failed", "not_read"):
        errs.append("read_failed")
    if e.get("missing_lines"):
        errs.append("missing_lines")
    h = content(text or "")
    if any(d["status"] == "approx" and content(d["text"]) not in h for d in e["line_detail"]):
        errs.append("char_error_line")
    if e.get("missing_digits") or e.get("extra_digits"):
        errs.append("digit_error")
    if e.get("added_segments"):
        errs.append("added_segment")
    if e.get("location_error_lines"):
        errs.append("location_error")
    if unit_eval and unit_eval["kept"] < unit_eval["units"]:
        errs.append("unit_broken")
    if row_eval and row_eval["kept"] < row_eval["rows"]:
        errs.append("table_row_broken")
    return errs


def check_case(case: dict, region: dict, unit_status: list[str] | None = None) -> dict:
    """fixtures/vlm-cases.json 사례 검사. 소속 영역 최종 문구 안에서만 본다."""
    text = norm(region.get("text") or "")
    fails = [f"missing:{s}" for s in case.get("must_contain", []) if norm(s) not in text]
    fails += [f"contains:{s}" for s in case.get("must_not_contain", []) if norm(s) in text]
    if case.get("must_read") and region["text_status"] != "read":
        fails.append(f"status:{region['text_status']}")
    if "table_rows_min" in case:
        n = len((region.get("table") or {}).get("rows") or [])
        if n < case["table_rows_min"]:
            fails.append(f"rows:{n}")
    if case.get("units_all_kept") and (unit_status is None or any(u != "kept" for u in unit_status)):
        fails.append(f"units:{unit_status}")
    return {"passed": not fails, "fails": fails, "flagged": bool(region["review"]["quality"])}
