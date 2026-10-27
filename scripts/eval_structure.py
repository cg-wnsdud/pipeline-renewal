"""여러 통합 실행의 구조·관계·OCR 추종·검토 표시 평가를 나란히 비교한다(모델 호출 없음).

- 문구 보존(eval/2.1 페이지·영역 단위)과 구조 보존(연결 단위·표 행)을 따로 센다.
- 사례 검사(fixtures/vlm-cases.json)와 '기준선 모든 실행에서 오류 없던 영역'의 유지 여부를 본다.
- 검토 표시: 영역별 평가상 오류 유무 × 품질 근거 표시 유무.

사용: eval_structure.py --out work/compare/x --baseline c1,c2 c1=work/pipeline/full-c1 c2=work/pipeline/full-c2 v2=...
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import structure_eval as se, text_eval  # noqa: E402
from layout_pipeline.gold_eval import load_gold  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402

OCR_CATS = ["both_correct", "vlm_corrected_ocr", "vlm_followed_ocr_error", "vlm_broke_correct_ocr",
            "both_wrong_differently", "no_ocr_vlm_ok", "no_ocr_vlm_wrong", "symbol_only_line"]


def evaluate_run(path: Path, structure: dict, cases: list[dict], only: set[str] | None = None) -> dict:
    result = load(path / "result.json")
    if only:
        result["pages"] = [p for p in result["pages"] if p["page_id"] in only]
        result["regions"] = [r for r in result["regions"] if r["page_id"] in only]
        result["unassigned_texts"] = [u for u in result["unassigned_texts"] if u["page_id"] in only]
    stage1 = ROOT / result["settings"]["stage1"]
    regs_by_page: dict[str, list] = {}
    for r in result["regions"]:
        regs_by_page.setdefault(r["page_id"], []).append(r)
    un_by_page: dict[str, list] = {}
    for u in result["unassigned_texts"]:
        un_by_page.setdefault(u["page_id"], []).append(u)
    by_id = {r["region_id"]: r for r in result["regions"]}
    regions_out, ocr_rows, page_totals = {}, [], Counter()
    for p in result["pages"]:
        if p["execution_status"] not in ("READY", "CANDIDATE_RUN"):
            continue
        gold, _ = load_gold(p["page_id"], p["image_sha256"], [p["width"], p["height"]])
        if gold is None:
            continue
        gold_by_id = {g["id"]: g for g in gold["lines"]}
        regs = regs_by_page.get(p["page_id"], [])
        rec = load(stage1 / "records" / f"{p['page_id']}.json")
        ocr_items = rec["assignments"] + rec["unassigned_texts"]
        pe = text_eval.evaluate_page(gold["lines"], regs, un_by_page.get(p["page_id"], []))
        for g in pe["lines"]:
            page_totals[(g["geometry"], g["status"])] += 1
        ocr_rows += se.ocr_vlm_outcomes(pe["lines"], gold_by_id, ocr_items, regs)
        pref = p["page_id"] + "-"
        for r in regs:
            e = text_eval.evaluate_region(r["text"], r["bbox"], gold["lines"])
            u = structure["units"].get(r["region_id"])
            ue = se.evaluate_units(r["text"], [[pref + g for g in unit] for unit in u["units"]], gold_by_id) if u else None
            t = structure["table_rows"].get(r["region_id"])
            te = (se.evaluate_table_rows((r.get("table") or {}).get("rows"), [[pref + g for g in row] for row in t["rows"]],
                                         gold_by_id) if t else None)
            errs = se.region_has_error(e, ue, te, r["text_status"], r["text"])
            regions_out[r["region_id"]] = {
                "errors": errs, "quality": r["review"]["quality"], "context": r["review"]["context"],
                "table_candidate": r["table_candidate"], "table_status": (r.get("table") or {}).get("status"),
                "expected_lines": e["expected_lines"], "lines_exact": e["lines_exact"], "lines_approx": e["lines_approx_only"],
                "missing_lines": len(e["missing_lines"]), "added_segments": e["added_segments"],
                "unsupported_chars": e["unsupported_chars"], "output_chars": e["output_chars"],
                "units": None if ue is None else {k: ue[k] for k in ("units", "kept", "order_or_block_mixed", "lines_missing")},
                "units_detail": None if ue is None else [d["status"] for d in ue["detail"]],
                "table_rows": None if te is None else {k: te[k] for k in ("rows", "kept", "output_rows")}}
    case_res = {c["id"]: se.check_case(c, by_id[c["region_id"]], (regions_out.get(c["region_id"]) or {}).get("units_detail"))
                for c in cases if c["region_id"] in by_id}
    return {"regions": regions_out, "ocr": ocr_rows, "page_totals": page_totals, "cases": case_res,
            "settings": {k: result["settings"].get(k) for k in ("general_request", "table_request", "vlm_concurrency")}}


def summarize(ev: dict) -> dict:
    regs = ev["regions"].values()
    units = [r["units"] for r in regs if r["units"]]
    rows = [r["table_rows"] for r in regs if r["table_rows"]]
    pt = ev["page_totals"]
    err = [r for r in regs if r["errors"]]
    ok = [r for r in regs if not r["errors"]]
    return {
        "page_lines": sum(pt.values()), "home_exact": pt[("home", "home_exact")], "home_approx": pt[("home", "home_approx")],
        "home_missing_or_elsewhere": sum(v for (g, k), v in pt.items() if g == "home" and not k.startswith("home_")),
        "regions": len(ev["regions"]), "read_failed": sum("read_failed" in r["errors"] for r in regs),
        "lines_exact": sum(r["lines_exact"] for r in regs), "lines_approx": sum(r["lines_approx"] for r in regs),
        "missing_lines": sum(r["missing_lines"] for r in regs), "expected_lines": sum(r["expected_lines"] for r in regs),
        "added_segments": sum(len(r["added_segments"]) for r in regs),
        "unsupported_chars": sum(r["unsupported_chars"] for r in regs), "output_chars": sum(r["output_chars"] for r in regs),
        "units": sum(u["units"] for u in units), "units_kept": sum(u["kept"] for u in units),
        "units_mixed": sum(u["order_or_block_mixed"] for u in units), "units_lines_missing": sum(u["lines_missing"] for u in units),
        "table_rows": sum(t["rows"] for t in rows), "table_rows_kept": sum(t["kept"] for t in rows),
        "ocr_content": Counter(r["content"] for r in ev["ocr"]), "ocr_strict": Counter(r["strict"] for r in ev["ocr"]),
        "review": {"error_flagged": sum(bool(r["quality"]) for r in err), "error_unflagged": sum(not r["quality"] for r in err),
                   "ok_flagged": sum(bool(r["quality"]) for r in ok), "ok_unflagged": sum(not r["quality"] for r in ok)},
        "error_types": Counter(t for r in err for t in r["errors"]),
        "error_types_flagged": Counter(t for r in err if r["quality"] for t in r["errors"]),
        "unneeded_flag_reasons": Counter(q for r in ok for q in r["quality"]),
        "cases_passed": sum(c["passed"] for c in ev["cases"].values()), "cases": len(ev["cases"]),
    }


def span(vals: list) -> str:
    return str(vals[0]) if len(set(vals)) == 1 else f"{min(vals)}~{max(vals)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="name=path")
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", required=True, help="기준선 실행 이름(쉼표 구분)")
    ap.add_argument("--pages", default="", help="이 page_id 들만 평가(쉼표 구분)")
    a = ap.parse_args()
    structure = load(ROOT / "fixtures" / "structure-gold.json")
    cases = load(ROOT / "fixtures" / "vlm-cases.json")["cases"]
    runs = dict(x.split("=", 1) for x in a.runs)
    base = a.baseline.split(",")
    only = {x for x in a.pages.split(",") if x} or None
    evs = {n: evaluate_run(ROOT / p, structure, cases, only) for n, p in runs.items()}
    sums = {n: summarize(e) for n, e in evs.items()}
    stable = sorted(set.intersection(*[{rid for rid, r in evs[n]["regions"].items() if not r["errors"]} for n in base]))
    names = list(runs)
    L = ["# 구조·관계·OCR 추종·검토 표시 비교", "",
         f"기준선(같은 지시 반복): {', '.join(base)}. 기준선 변동 폭은 '기준선 범위' 열. 실행 설정: "
         + "; ".join(f"{n}={evs[n]['settings']}" for n in names), "",
         "## 1. 문구 보존(eval/2.1)과 구조 보존", "",
         "| 지표 | " + " | ".join(names) + " | 기준선 범위 |", "|---|" + "---:|" * (len(names) + 1)]

    def row(label, f):
        vals = [f(sums[n]) for n in names]
        L.append(f"| {label} | " + " | ".join(str(v) for v in vals) + f" | {span([f(sums[n]) for n in base])} |")
    row("페이지 정답 줄: 소속 영역 정확", lambda s: s["home_exact"])
    row("페이지 정답 줄: 소속 영역 근사", lambda s: s["home_approx"])
    row("페이지 정답 줄(home): 소속 영역 밖·누락", lambda s: s["home_missing_or_elsewhere"])
    row("영역 판독 실패", lambda s: s["read_failed"])
    row("영역 기대 줄 누락", lambda s: s["missing_lines"])
    row("근거 없는 글자", lambda s: s["unsupported_chars"])
    row("추가 구간(4자 이상)", lambda s: s["added_segments"])
    row(f"연결 단위 보존(/{sums[base[0]]['units']})", lambda s: s["units_kept"])
    row("연결 단위: 줄은 있으나 순서·블록 혼합", lambda s: s["units_mixed"])
    row("연결 단위: 줄 누락", lambda s: s["units_lines_missing"])
    row(f"표 행 대응 보존(/{sums[base[0]]['table_rows']})", lambda s: s["table_rows_kept"])
    L += ["", "## 2. OCR 대비 결과(소속 영역이 있는 정답 줄, 같은 위치 OCR 과 비교)", "",
          "| 분류 | " + " | ".join(names) + " | 기준선 범위 |", "|---|" + "---:|" * (len(names) + 1)]
    for lv, key in (("글자·숫자", "ocr_content"), ("기호 포함", "ocr_strict")):
        for c in OCR_CATS:
            row(f"{c} ({lv})", lambda s, c=c, key=key: s[key].get(c, 0))
    L += ["", "- vlm_followed_ocr_error: VLM 이 틀렸고 OCR 오답 원문이 소속 영역 VLM 문구에 그대로 있음. vlm_broke_correct_ocr: OCR 은 맞는데 VLM 이 틀림(생략 포함).",
          "- 글자·숫자 수준 = 글자·숫자·% 만 비교(글머리 기호·문장부호 차이 제외). 기호 포함 수준 = 정규화(공백·'|' 제거, 기호 통일) 후 비교.",
          "",
          "## 3. 검토 표시(품질 근거)와 평가상 오류", "",
          "| 지표 | " + " | ".join(names) + " | 기준선 범위 |", "|---|" + "---:|" * (len(names) + 1)]
    row("오류 있음·표시함", lambda s: s["review"]["error_flagged"])
    row("오류 있음·표시 안 함(누락)", lambda s: s["review"]["error_unflagged"])
    row("오류 없음·표시함(불필요 가능)", lambda s: s["review"]["ok_flagged"])
    row("오류 없음·표시 안 함", lambda s: s["review"]["ok_unflagged"])
    L += ["", "오류 유형별(표시된 수/전체):", ""]
    for n in names:
        s = sums[n]
        L.append(f"- {n}: " + ", ".join(f"{t} {s['error_types_flagged'].get(t, 0)}/{v}" for t, v in sorted(s["error_types"].items()))
                 + f"; 오류 없는 영역의 표시 근거 {dict(s['unneeded_flag_reasons'])}")
    L += ["", "오류 판정 근거: 판독 실패, 기대 줄 누락, 근사 줄(글자 오류), 숫자 누락·추가, 추가 구간(4자 이상, 정답 누락 가능), "
          "위치 오류 줄, 연결 단위 깨짐, 표 행 깨짐. 정답 자료에 없는 실제 문구(배지 등)는 추가 구간으로 잡힐 수 있다.", "",
          "## 4. 사례(fixtures/vlm-cases.json)", "", "| 사례 | 종류 | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for c in cases:
        cells = []
        for n in names:
            r = evs[n]["cases"].get(c["id"])
            cells.append("-" if r is None else ("통과" if r["passed"] else "실패 " + ",".join(r["fails"]))
                         + ("" if r is None else (" [표시]" if r["flagged"] else "")))
        L.append(f"| {c['id']} | {c['kind']} | " + " | ".join(cells) + " |")
    L += ["", f"## 5. 기준선 모든 실행에서 오류 없던 영역 {len(stable)}개의 유지", ""]
    for n in names:
        broke = [rid for rid in stable if evs[n]["regions"].get(rid, {}).get("errors")]
        L.append(f"- {n}: 오류 발생 {len(broke)}" + (f" {[(rid, evs[n]['regions'][rid]['errors']) for rid in broke]}" if broke else ""))
    L += ["", "## 6. 연결 단위·표 행 영역별", ""]
    for rid in list(structure["units"]) + list(structure["table_rows"]):
        cells = []
        for n in names:
            r = evs[n]["regions"].get(rid)
            if r is None:
                cells.append("-")
            elif r["units"]:
                cells.append(f"{r['units']['kept']}/{r['units']['units']} {r['units_detail']}")
            else:
                cells.append(f"행 {r['table_rows']['kept']}/{r['table_rows']['rows']} (출력 {r['table_rows']['output_rows']}행, {r['table_status']})")
        L.append(f"- {rid}: " + " | ".join(f"{n}: {c}" for n, c in zip(names, cells)))
    md = "\n".join(L) + "\n"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "structure.md").write_text(md, encoding="utf-8")
    dump(out / "structure.json", {"runs": runs, "baseline": base, "stable_regions": stable,
                                  "summaries": {n: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in s.items()} for n, s in sums.items()},
                                  "regions": {n: e["regions"] for n, e in evs.items()}, "cases": {n: e["cases"] for n, e in evs.items()},
                                  "ocr": {n: [r for r in e["ocr"] if r["strict"] != "both_correct" or r["content"] != "both_correct"] for n, e in evs.items()}})
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
