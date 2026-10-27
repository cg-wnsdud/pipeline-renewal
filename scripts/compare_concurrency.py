"""동시성 비교 요약(모델 호출 없음). 동일 작업량의 OCR 단독·VLM 단독·혼합 실행을 나란히 놓는다.

OCR: work/runs/ocr-c1, ocr-c2, ocr-c4, ocr-c1-mixed (16페이지 전체 입력 OCR, 기준 출력 = ocr-c1)
VLM: work/pipeline/full-c1, full-c2, full-c4, full-c2-mixed (같은 Stage 1, 200영역, 기준 출력 = full-c1)
결과: work/compare/concurrency.md / .json
"""
from __future__ import annotations

import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.text_eval import norm  # noqa: E402
from layout_pipeline.stage1 import resolve  # noqa: E402


def ocr_signature(run: Path) -> dict:
    sig = {}
    for c in load(run / "calls.json"):
        if not c["ok"]:
            continue
        p = load(resolve(c["raw_path"]))["result"]["layoutParsingResults"][0]["prunedResult"]
        sig[c["page_id"]] = ([b["coordinate"] for b in p["layout_det_res"]["boxes"]], p["overall_ocr_res"]["rec_texts"])
    return sig


def main() -> int:
    rows_ocr, rows_vlm = [], []
    base_sig = ocr_signature(ROOT / "work/runs/ocr-c1")
    for name in ("ocr-c1", "ocr-c2", "ocr-c4", "ocr-c1-mixed", "ocr-c1-mixed-nc"):
        run = ROOT / "work/runs" / name
        if not (run / "run.json").exists():
            continue
        meta, calls = load(run / "run.json"), load(run / "calls.json")
        sig = ocr_signature(run)
        rows_ocr.append({"run": name, "concurrency": meta.get("ocr_concurrency"), "ocr_wall_s": meta.get("ocr_wall_seconds"),
                         "total_wall_s": meta.get("wall_seconds"), "calls": len(calls), "failed": sum(not c["ok"] for c in calls),
                         "call_median_s": round(median(c["seconds"] for c in calls), 3),
                         "call_max_s": round(max(c["seconds"] for c in calls), 3),
                         "pages_output_identical_to_c1": sum(sig.get(k) == v for k, v in base_sig.items()), "pages": len(base_sig)})
    for group, names in (("cached", ("full-c1", "full-c2", "full-c4", "full-c2-mixed")),
                         ("cache_bust", ("nc-c1", "nc-c2", "nc-c4", "nc-c2-mixed"))):
        if not (ROOT / "work/pipeline" / names[0] / "result.json").exists():
            continue
        base = {r["region_id"]: r for r in load(ROOT / "work/pipeline" / names[0] / "result.json")["regions"]}
        for name in names:
            out = ROOT / "work/pipeline" / name
            if not (out / "result.json").exists():
                continue
            res, timing = load(out / "result.json"), load(out / "trace/timing.json")
            ev = load(out / "eval/eval.json") if (out / "eval/eval.json").exists() else {}
            calls = [load(p) for p in (out / "trace/vlm").glob("*.json")]
            regs = {r["region_id"]: r for r in res["regions"]}
            same = sum(regs[k]["text"] == v["text"] for k, v in base.items() if k in regs)
            same_norm = sum(norm(regs[k]["text"] or "") == norm(v["text"] or "") for k, v in base.items() if k in regs)
            pt = ev.get("page_totals", {})
            gen = (ev.get("summary") or {}).get("general") or {}
            rows_vlm.append({"group": group, "run": name, "concurrency": timing["read"]["vlm_concurrency"],
                             "read_wall_s": timing["read"]["wall_seconds"], "http_calls": timing["read"]["http_calls"],
                             "call_median_s": round(median(c["seconds"] for c in calls), 2) if calls else None,
                             "call_max_s": round(max(c["seconds"] for c in calls), 2) if calls else None,
                             "finish_not_stop": sum(c["finish_reason"] != "stop" for c in calls),
                             "read_failed": sum(r["text_status"] == "read_failed" for r in res["regions"]),
                             "contract_passed": res["contract_checks"]["passed"],
                             "text_identical_to_base": same, "text_identical_normalized": same_norm, "regions": len(base),
                             "page_home_exact": pt.get("home:home_exact"),
                             "page_missing": pt.get("home:missing", 0) + pt.get("no_home:missing", 0),
                             "general_lines_exact": gen.get("lines_exact"), "general_lines_missing": gen.get("lines_missing"),
                             "general_unsupported_chars": gen.get("unsupported_chars")})
    md = ["# 동시성 비교(동일 작업량)", "",
          "공유 GPU(GB10 1대에 vLLM·PaddleX 등). 다른 사용자의 부하는 통제하지 않았다. 각 조건 1회 측정.", "",
          "## OCR 단독(16페이지 전체 입력, 서버 workers=1 기록)", "",
          "| 실행 | 동시성 | OCR 벽시계(초) | 전체(렌더·HWP 변환 포함) | 호출 | 실패 | 호출 중앙값/최대(초) | c1 과 출력 동일 페이지 |",
          "|---|---:|---:|---:|---:|---:|---|---|"]
    for r in rows_ocr:
        md.append(f"| {r['run']} | {r['concurrency']} | {r['ocr_wall_s']} | {r['total_wall_s']} | {r['calls']} | {r['failed']} | "
                  f"{r['call_median_s']}/{r['call_max_s']} | {r['pages_output_identical_to_c1']}/{r['pages']} |")
    md += ["", "## VLM 판독(같은 Stage 1, 200영역)", "",
           "같은 묶음의 첫 실행(c1)을 기준으로 문구 동일 여부를 센다. cached = 같은 요청을 반복 실행(서버 prefix cache 영향 가능), "
           "cache_bust = 요청마다 고유 식별자.", "",
           "| 묶음 | 실행 | 동시성 | 판독 벽시계(초) | HTTP | 호출 중앙값/최대(초) | finish≠stop | 판독 실패 | 계약 | 기준과 문구 동일(원문/정규화) | 페이지 평가: 소속 정확 / 누락 | 일반 영역: 정확 / 누락 / 근거 없는 글자 |",
           "|---|---|---:|---:|---:|---|---:|---:|---|---|---|---|"]
    for r in rows_vlm:
        md.append(f"| {r['group']} | {r['run']} | {r['concurrency']} | {r['read_wall_s']} | {r['http_calls']} | {r['call_median_s']}/{r['call_max_s']} | "
                  f"{r['finish_not_stop']} | {r['read_failed']} | {'통과' if r['contract_passed'] else '실패'} | "
                  f"{r['text_identical_to_base']}/{r['text_identical_normalized']} ({r['regions']}) | {r['page_home_exact']} / {r['page_missing']} | "
                  f"{r['general_lines_exact']} / {r['general_lines_missing']} / {r['general_unsupported_chars']} |")
    text = "\n".join(md) + "\n"
    out = ROOT / "work/compare"
    out.mkdir(parents=True, exist_ok=True)
    (out / "concurrency.md").write_text(text, encoding="utf-8")
    dump(out / "concurrency.json", {"ocr": rows_ocr, "vlm": rows_vlm})
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
