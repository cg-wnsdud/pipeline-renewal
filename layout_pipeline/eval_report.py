"""영역 평가 집계와 요약 표(eval/2 지표). VLM 실험과 통합 실행 평가가 함께 쓴다."""
from __future__ import annotations

from collections import Counter

METRIC_NOTE = ("정답→출력: 정확(정규화 후 완전 일치)/근사(줄 길이 10% 이하 편집)/누락. recall 측 편집 비율은 출력의 추가 글자를 "
               "세지 않으므로 표준 CER 이 아니다. 출력→원본: 3글자 조각 근거로 근거 없는 글자(추가 구간 4자 이상 + 짧은 글자 오류)와 "
               "다른 영역 문구(위치 오류)를 센다. 정답에 없는 실제 문구가 있을 수 있어 근거 없음은 오류 후보다.")


def aggregate(rows: list[dict]) -> dict:
    ev = [r["eval"] for r in rows if r.get("eval")]
    chars = sum(sum(len(d["text"]) for d in e["line_detail"]) for e in ev)
    errs = sum(sum(d["edit_distance"] for d in e["line_detail"]) for e in ev)
    out_chars = sum(e["output_chars"] for e in ev)
    unsupported = sum(e["unsupported_chars"] for e in ev)
    reasons_q = Counter(x for r in rows for x in r["review"]["quality_reasons"])
    reasons_c = Counter(x for r in rows for x in r["review"]["context_reasons"])
    return {
        "regions": len(rows), "contract_ok": sum(bool(r.get("contract_ok")) for r in rows),
        "read_failed": sum(r.get("text") is None for r in rows), "empty_text": sum(r.get("text") == "" for r in rows),
        "uncertain_true": sum(r.get("uncertain") is True for r in rows),
        "expected_lines": sum(e["expected_lines"] for e in ev),
        "lines_exact": sum(e["lines_exact"] for e in ev), "lines_approx_only": sum(e["lines_approx_only"] for e in ev),
        "lines_missing": sum(len(e["missing_lines"]) for e in ev),
        "recall_side_edit_rate": round(errs / chars, 4) if chars else None,
        "output_chars": out_chars, "unsupported_chars": unsupported,
        "unsupported_rate": round(unsupported / out_chars, 4) if out_chars else None,
        "added_segments": sum(len(e["added_segments"]) for e in ev),
        "regions_with_added_segments": sum(bool(e["added_segments"]) for e in ev),
        "char_error_runs": sum(e["char_error_runs"] for e in ev),
        "location_error_chars": sum(e["location_error_chars"] for e in ev),
        "location_error_lines": sum(len(e["location_error_lines"]) for e in ev),
        "missing_digit_tokens": sum(len(e["missing_digits"]) for e in ev),
        "extra_digit_tokens": sum(len(e["extra_digits"]) for e in ev),
        "duplicated_lines": sum(len(e["duplicated_lines"]) for e in ev),
        "needs_review": sum(r["review"]["needs_review"] for r in rows),
        "needs_review_quality": sum(bool(r["review"]["quality_reasons"]) for r in rows),
        "review_quality_reasons": dict(reasons_q), "review_context_reasons": dict(reasons_c),
    }


HEADER = ("| 조건 | 영역 | 계약 통과 | 판독 실패 | 정답 줄: 정확 / 근사만 / 누락 (전체) | recall 측 편집 비율 | "
          "출력 글자 | 근거 없는 글자(비율) | 추가 구간(영역 수) | 짧은 글자 오류 구간 | 위치 오류 글자/줄 | 숫자 누락/추가 | 영역 내 중복 줄 | 검토(품질 근거) |")
SEP = "|---|---:|---:|---:|---|---:|---:|---|---|---:|---|---|---:|---|"


def row_md(name: str, a: dict) -> str:
    return (f"| {name} | {a['regions']} | {a['contract_ok']} | {a['read_failed']} | "
            f"{a['lines_exact']} / {a['lines_approx_only']} / {a['lines_missing']} ({a['expected_lines']}) | "
            f"{a['recall_side_edit_rate']} | {a['output_chars']} | {a['unsupported_chars']} ({a['unsupported_rate']}) | "
            f"{a['added_segments']} ({a['regions_with_added_segments']}) | {a['char_error_runs']} | "
            f"{a['location_error_chars']}/{a['location_error_lines']} | {a['missing_digit_tokens']}/{a['extra_digit_tokens']} | "
            f"{a['duplicated_lines']} | {a['needs_review']}({a['needs_review_quality']}) |")
