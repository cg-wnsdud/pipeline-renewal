"""재판독 조건 제거 효과의 오프라인 채점(모델 호출 0). 기존 실행 폴더는 읽기만 한다.

지정한 재판독 사유'만'으로 재판독된 영역에서 최종 문구를 1차 판독 문구로 되돌린 반사실(counterfactual) 결과를 만들고,
정답이 있는 페이지(페이지 이미지 SHA-256 이 fixtures/gold/page-sha.json 과 같은 페이지)에서 영역 단위로 현재 결과와 비교한다.

사용: eval_reread_ablation.py --run <실행> [--run <실행> ...] --only-reason small_text_after_resize --out <새 폴더>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import text_eval  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402

GOLD_DIR = ROOT / "fixtures" / "gold"
KEYS = ("lines_exact", "lines_approx_only", "unsupported_chars", "char_error_runs", "location_error_chars")


def gold_by_sha() -> dict[str, str]:
    return {sha: pid for pid, sha in load(GOLD_DIR / "page-sha.json").items()}


def measures(e: dict) -> dict:
    return {"expected": e["expected_lines"], "exact": e["lines_exact"], "approx_only": e["lines_approx_only"],
            "missing": len(e["missing_lines"]), "unsupported_chars": e["unsupported_chars"],
            "char_error_runs": e["char_error_runs"], "location_error_chars": e["location_error_chars"],
            "missing_digits": len(e["missing_digits"]), "extra_digits": len(e["extra_digits"]),
            "duplicated_lines": len(e["duplicated_lines"])}


def verdict(cur: dict, alt: dict) -> str:
    """현재(재판독 반영) 대비 반사실(1차 유지)이 나쁜지. 누락·오류가 늘면 worse."""
    bad = ("missing", "unsupported_chars", "char_error_runs", "location_error_chars", "missing_digits", "extra_digits",
           "duplicated_lines")
    worse = any(alt[k] > cur[k] for k in bad) or alt["exact"] < cur["exact"]
    better = any(alt[k] < cur[k] for k in bad) or alt["exact"] > cur["exact"]
    return "same" if not worse and not better else ("worse_without_reread" if worse and not better
                                                     else ("better_without_reread" if better and not worse else "mixed"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", type=Path, required=True)
    ap.add_argument("--only-reason", default="small_text_after_resize")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists() and any(a.out.iterdir()):
        ap.error(f"출력 폴더가 비어 있지 않습니다: {a.out}")
    sha2gold = gold_by_sha()
    report = {"only_reason": a.only_reason, "runs": {}}
    lines = [f"# 재판독 사유 `{a.only_reason}` 단독 재판독을 하지 않았을 때(1차 문구 유지)의 정답 대조", "",
             "모델 호출 0. 현재 = 실행 결과 그대로, 반사실 = 해당 영역만 1차 판독 문구로 되돌림. 정답이 있는 READY 페이지만.", ""]
    for run in a.run:
        result = load(run / "result.json")
        reads = load(run / "trace" / "reads.json")
        regions_by_page: dict[str, list] = {}
        for r in result["regions"]:
            regions_by_page.setdefault(r["page_id"], []).append(r)
        rows, counts = [], {"target_regions": 0, "target_no_gold": 0, "first_text_missing": 0}
        for p in result["pages"]:
            gold_pid = sha2gold.get(p["image_sha256"])
            for r in regions_by_page.get(p["page_id"], []):
                tr = (reads.get(r["region_id"]) or {}).get("trace") or {}
                if "reread" not in (tr.get("kinds") or []) or tr.get("verify_reasons") != [a.only_reason]:
                    continue
                counts["target_regions"] += 1
                if gold_pid is None or p["execution_status"] != "READY":
                    counts["target_no_gold"] += 1
                    continue
                first = tr.get("first_text")
                if first is None:
                    counts["first_text_missing"] += 1
                    continue
                gold = load(GOLD_DIR / f"{gold_pid}.json")
                if gold["ref_size"] != [p["width"], p["height"]]:
                    counts["target_no_gold"] += 1
                    continue
                cur = measures(text_eval.evaluate_region(r["text"], r["bbox"], gold["lines"]))
                alt = measures(text_eval.evaluate_region(first, r["bbox"], gold["lines"]))
                rows.append({"region_id": r["region_id"], "gold_page": gold_pid, "reread_source": tr.get("reread_source"),
                             "text_changed": text_eval.norm(first) != text_eval.norm(r["text"] or ""),
                             "current": cur, "without_reread": alt, "verdict": verdict(cur, alt),
                             "first_text": first, "final_text": r["text"]})
        tally: dict[str, int] = {}
        for x in rows:
            tally[x["verdict"]] = tally.get(x["verdict"], 0) + 1
        tot = {k: (sum(x["current"][k] for x in rows), sum(x["without_reread"][k] for x in rows))
               for k in ("expected", "exact", "approx_only", "missing", "unsupported_chars", "char_error_runs",
                         "location_error_chars", "missing_digits", "extra_digits", "duplicated_lines")}
        report["runs"][str(run)] = {"counts": counts, "evaluated": len(rows), "verdicts": tally, "totals": tot, "regions": rows}
        lines += [f"## {run}", "",
                  f"- 대상(사유가 `{a.only_reason}` 하나뿐인 재판독 영역): {counts['target_regions']}개, 정답 대조 {len(rows)}개"
                  f"(정답 없음·READY 아님 {counts['target_no_gold']}, 1차 문구 없음 {counts['first_text_missing']})",
                  f"- 판정: {tally}", "",
                  "| 지표(합계) | 현재(재판독 반영) | 재판독 안 함(1차 유지) |", "|---|---:|---:|"]
        lines += [f"| {k} | {v[0]} | {v[1]} |" for k, v in tot.items()]
        diff = [x for x in rows if x["verdict"] != "same"]
        if diff:
            lines += ["", "차이가 난 영역:", ""]
            for x in diff:
                c, w = x["current"], x["without_reread"]
                lines.append(f"- {x['region_id']}({x['gold_page']}) {x['verdict']}: "
                             + ", ".join(f"{k} {c[k]}→{w[k]}" for k in c if c[k] != w[k]))
        lines.append("")
    dump(a.out / "ablation.json", report)
    (a.out / "ablation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
