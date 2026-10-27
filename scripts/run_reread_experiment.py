"""영역 재판독(region-reread/1) 작은 비교(docs/10). 기존 Stage 1·1차 page/band 응답을 재사용한다.

사용:
  run_reread_experiment.py --run work/pipeline/page-first-v2-2026-10-26 --set core --conditions A,B --reps 2 --out work/reread/exp1
  run_reread_experiment.py --run ... --set control --conditions B --reps 2 --out work/reread/exp1
  run_reread_experiment.py --run ... --report --out work/reread/exp1
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import page_read, text_eval  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.gold_eval import load_gold  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.structure_eval import content, evaluate_units  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402

N = text_eval.norm


def context(run: Path, rid: str, reads: dict) -> tuple[str | None, dict]:
    tr = reads[rid]["trace"]
    first_call = load(run / "trace" / "vlm" / f"{tr['calls'][0]}.json")
    meta = first_call["request_meta"]
    assert meta["kind"] == "page", f"{rid}: 첫 호출이 페이지 판독이 아님"
    view = {"view_id": meta["view_id"], "crop": meta["view_crop"], "band_of": meta.get("band_of")}
    return tr.get("page_text"), view


def check(case: dict, text: str | None, rows=None, units=None) -> list[str]:
    t = N(text or "")
    fails = []
    for s in case.get("must_contain", []):
        if N(s) not in t:
            fails.append(f"누락:{s}")
    for s in case.get("must_not_contain", []):
        if N(s) in t:
            fails.append(f"유입·잘못된 문구:{s}")
    for s, n in (case.get("count") or {}).items():
        if t.count(N(s)) != n:
            fails.append(f"횟수:{s}={t.count(N(s))}(원본 {n})")
    for s in case.get("line_exists", []):
        if not any(N(x) == N(s) for x in (text or "").splitlines()):
            fails.append(f"제목 줄 없음:{s}")
    bo = case.get("block_order")
    if bo:
        lpos = [t.rfind(N(s)) for s in bo["left"]]
        rpos = [t.find(N(s)) for s in bo["right"]]
        if min(lpos + rpos) >= 0 and max(lpos) > min(rpos):
            fails.append("좌우 블록 섞임")
    if "exact" in case and t != N(case["exact"]):
        fails.append("항목명→값 불일치")
    if case.get("rows_cover_text_min") and rows:
        cov = text_eval.similarity_cover(text or "", "\n".join(" ".join(r) for r in rows))
        if cov < case["rows_cover_text_min"]:
            fails.append(f"rows 와 모순(덮임 {cov:.2f})")
    if units is not None and any(u != "kept" for u in units):
        fails.append(f"연결 단위:{units}")
    return fails


def gold_profile(text: str | None, bbox, gold_lines) -> dict:
    e = text_eval.evaluate_region(text, bbox, gold_lines)
    h = content(text or "")
    return {"missing": len(e["missing_lines"]), "location": len(e["location_error_lines"]),
            "char": sum(1 for d in e["line_detail"] if d["status"] == "approx" and content(d["text"]) not in h),
            "added": len(e["added_segments"])}


def run_calls(a, cases: list[dict]) -> None:
    load_env_file()
    run = ROOT / a.run
    reads = load(run / "trace" / "reads.json")
    result = load(run / "result.json")
    stage1 = ROOT / result["settings"]["stage1"]
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                       prefix=f"rr-{a.set}")
    out = ROOT / a.out
    for rep in range(1, a.reps + 1):
        for case in cases:
            rid = case["region_id"]
            page = rid.split("-")[0]
            rec = load(stage1 / "records" / f"{page}.json")
            region = next(r for r in rec["regions"] if r["region_id"] == rid)
            img = Image.open(rec["page"]["image_path"]).convert("RGB")
            first, view = context(run, rid, reads)
            for cond in a.conditions.split(","):
                req = page_read.reread_request(rec, region, img, first, view if cond == "B" else None)
                t0 = time.perf_counter()
                call = client.chat(req["text"], req["images"], req["schema"], "region_reread", page_read.REREAD_MAX_TOKENS,
                                   {"kind": "reread", "version": req["version"], "condition": cond, "rep": rep, "region_ids": [rid],
                                    "page_id": page, "context_view": view if cond == "B" else None, "image_roles": req["image_roles"]})
                rec_call = asdict(call) | {"wall_seconds": round(time.perf_counter() - t0, 2)}
                dump(out / "calls" / f"{a.set}-{cond}-r{rep}-{rid}.json", rec_call)
                print(a.set, cond, rep, rid, call.finish_reason, call.error, rec_call["wall_seconds"], flush=True)


def report(a) -> None:
    run = ROOT / a.run
    reads = load(run / "trace" / "reads.json")
    result = load(run / "result.json")
    final = {g["region_id"]: g for g in result["regions"]}
    stage1 = ROOT / result["settings"]["stage1"]
    spec = load(ROOT / "fixtures" / "reread-cases.json")
    sg = load(ROOT / "fixtures" / "structure-gold.json")
    out = ROOT / a.out
    rows_out, lines = [], ["# 영역 재판독 작은 비교", "", "계획: docs/10. 판정: fixtures/reread-cases.json(실행 전 고정).", ""]
    from types import SimpleNamespace
    for set_name in ("core", "control"):
        for case in spec[set_name]:
            rid = case["region_id"]
            page = rid.split("-")[0]
            rec = load(stage1 / "records" / f"{page}.json")
            region = next(r for r in rec["regions"] if r["region_id"] == rid)
            pg = next(p for p in result["pages"] if p["page_id"] == page)
            gold, _ = load_gold(page, pg["image_sha256"], [pg["width"], pg["height"]])
            gold_by = {g["id"]: g for g in gold["lines"]}
            u = sg["units"].get(rid)
            unit_ids = [[f"{page}-{x}" for x in un] for un in u["units"]] if u else None
            first, _ = context(run, rid, reads)
            rows = ((final[rid].get("table") or {}).get("rows"))

            def ev(label, text, extra=None):
                units = [d["status"] for d in evaluate_units(text, unit_ids, gold_by)["detail"]] if unit_ids and case.get("units_all_kept") else None
                fails = check(case, text, rows, units)
                gp = gold_profile(text, region["bbox"], gold["lines"])
                rows_out.append({"set": set_name, "region_id": rid, "label": label, "fails": fails, "gold": gp, "text": text} | (extra or {}))
            ev("1차(page)", first)
            ev("현재 최종(region-patch)", final[rid]["text"])
            for f in sorted((out / "calls").glob(f"{set_name}-*-{rid}.json")):
                c = load(f)
                meta = c["request_meta"]
                parsed = page_read.validate_reread(SimpleNamespace(error=c["error"], parsed=c["parsed"]), {"region_ids": [rid]})[rid]
                sel = page_read.select_reread(rec, region, first, parsed)
                ev(f"{meta['condition']} r{meta['rep']} 재판독", parsed["text"],
                   {"seconds": c["seconds"], "changes": parsed.get("changes"), "notes": parsed.get("notes"), "contract_ok": parsed["contract_ok"]})
                ev(f"{meta['condition']} r{meta['rep']} 선택({sel['source']})", sel["text"], {"selection": sel})
    dump(out / "report.json", rows_out)
    for set_name in ("core", "control"):
        lines += [f"## {set_name}", "", "| 영역 | 조건 | 판정 실패 | 정답 대조(누락/위치 오류/글자 오류/추가 구간) | 시간(s) |", "|---|---|---|---|---:|"]
        for r in rows_out:
            if r["set"] != set_name:
                continue
            g = r["gold"]
            lines.append(f"| {r['region_id'].replace('-v0', '')} | {r['label']} | {'; '.join(r['fails']) or '통과'} | "
                         f"{g['missing']}/{g['location']}/{g['char']}/{g['added']} | {r.get('seconds', '')} |")
        lines.append("")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--set", choices=["core", "control"], default="core")
    ap.add_argument("--conditions", default="A,B")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        report(a)
        return 0
    cases = load(ROOT / "fixtures" / "reread-cases.json")[a.set]
    run_calls(a, cases)
    report(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
