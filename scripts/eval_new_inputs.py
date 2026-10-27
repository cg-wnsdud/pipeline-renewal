"""새 입력 작은 정답(fixtures/new-inputs-gold.json) 대조(평가 전용, 모델 호출 없음). docs/11 §4.

사용: eval_new_inputs.py --run <out_dir> [--run ...] --out <dir>
각 실행의 result.json 을 영역별로 평가하고 실행끼리 나란히 적는다(문구 보존·순서·유입·기호·공백·표 구조·검토 표시를 따로).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.region_gold_eval import evaluate  # noqa: E402


def summarize(ev: dict) -> dict:
    tabs = [e["table"] for e in ev.values() if e.get("table")]
    return {"regions": len(ev),
            "regions_with_error": sum(e["has_error"] for e in ev.values()),
            "missing_lines": sum(len(e["missing_lines"]) for e in ev.values()),
            "order_errors": sum(not e["order_ok"] for e in ev.values()),
            "added_segments": sum(len(e["added_segments"]) for e in ev.values()),
            "char_errors": sum(e["char_errors"] for e in ev.values()),
            "stray_chars": sum(len(e["stray_chars"]) for e in ev.values()),
            "symbol_errors": sum(e["symbol_errors"] or 0 for e in ev.values()),
            "spacing_errors": sum(len(e["spacing_errors"]) for e in ev.values()),
            "tables_ok": f"{sum(t['ok'] for t in tabs)}/{len(tabs)}",
            "error_flagged_quality": sum(e["has_error"] and bool(e["quality"]) for e in ev.values()),
            "error_not_flagged": sum(e["has_error"] and not e["quality"] for e in ev.values()),
            "quality_flag_without_error": sum((not e["has_error"]) and bool(e["quality"]) for e in ev.values())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, action="append", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gold", type=Path, default=ROOT / "fixtures" / "new-inputs-gold.json")
    a = ap.parse_args()
    gold = load(a.gold)
    evs = {run.name: evaluate(gold, load(run / "result.json")) for run in a.run}
    sums = {k: summarize(v) for k, v in evs.items()}
    dump(a.out / "eval.json", {"gold": str(a.gold), "reviewer": gold["reviewer"], "summary": sums, "regions": evs})
    names = list(evs)
    lines = ["# 새 입력 작은 정답 대조", "", f"정답: {a.gold.name} ({gold['reviewer']}). 평가 전용.", "",
             "## 합계", "", "| 항목 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for key in next(iter(sums.values())):
        lines.append(f"| {key} | " + " | ".join(str(sums[n][key]) for n in names) + " |")
    lines += ["", "## 오류가 있는 영역(실행별)", "", "| 영역 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for rid in gold["regions"]:
        cells = []
        for n in names:
            e = evs[n].get(rid)
            if e is None:
                cells.append("-")
                continue
            bits = []
            if e["missing_lines"]:
                bits.append(f"누락 {len(e['missing_lines'])}")
            if not e["order_ok"]:
                bits.append("순서")
            if e["added_segments"]:
                bits.append("추가 " + ",".join(s[:12] for s in e["added_segments"]))
            if e["char_errors"]:
                bits.append(f"글자 {e['char_errors']}")
            if e["stray_chars"]:
                bits.append("원본에 없는 기호 " + "".join(e["stray_chars"]))
            t = e.get("table")
            if t and not t["ok"]:
                bits.append("표 " + ",".join([f"행혼합{t.get('rows_mixed')}" if t.get("rows_mixed") else ""]
                                             + [k for k, v in (t.get("relations") or {}).items() if v is False]
                                             + ([f"셀 {t['cells_exact']}/{t['cells_gold']}"] if "cells_exact" in t else [])).strip(","))
            if t and t.get("structure_not_expressible"):
                bits.append("병합 표현 불가(rows) " + ",".join(t["structure_not_expressible"]))
            flag = "Q:" + ",".join(e["quality"]) if e["quality"] else "표시없음"
            cells.append(("; ".join(bits) + f" ({flag})") if bits else ("" if not e["quality"] else f"정상 ({flag})"))
        if any(cells):
            lines.append(f"| {rid.replace('_p01', '')} | " + " | ".join(cells) + " |")
    lines += ["", "공백 차이(유무만, 줄바꿈·기호 옆 제외)와 기호 차이는 eval.json 의 spacing_errors·symbol_errors 에 영역별로 있다."]
    (a.out / "eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
