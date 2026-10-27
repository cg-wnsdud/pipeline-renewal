"""통합 실행 결과 평가(정답이 있는 페이지만). 사용자 결과(result.json)와 분리된 <out>/eval/ 에 저장한다.

- 영역 단위(eval/2): 일반 영역과 표 후보를 따로 집계. 정답→출력 누락, 출력→원본 근거 없는 글자·추가 구간·위치 오류.
- 페이지 단위: 페이지의 모든 정답 줄(선택 제외)을 분모로, 소속 영역 문구/다른 영역/미배정 OCR 에만/누락으로 분류.
- 표: 라벨(VLM 실행 전 고정, bbox 로 매칭)과 is_table 비교, gold-relations 표의 조건-값 대응.

사용: eval_pipeline.py <pipeline_out>
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import eval_report, text_eval, vlm_tasks  # noqa: E402
from layout_pipeline.final_viewer import build_final_viewer  # noqa: E402
from layout_pipeline.geometry import area, intersection  # noqa: E402
from layout_pipeline.gold_eval import load_gold  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402


def iou(a, b) -> float:
    i = intersection(a, b)
    return i / (area(a) + area(b) - i) if i else 0.0


def table_labels() -> list[dict]:
    """fixtures/vlm-eval-set.json 의 표 라벨(stage1-merge-large Region ID 기준)을 bbox 와 함께 읽는다."""
    spec = load(ROOT / "fixtures" / "vlm-eval-set.json")
    src = ROOT / spec["region_source_run"] / "records"
    out = []
    for rid, lab in spec["table_labels"]["labels"].items():
        rec = load(src / f"{rid.split('-v')[0]}.json")
        reg = next(r for r in rec["regions"] if r["region_id"] == rid)
        out.append({"page_id": rec["page"]["page_id"], "bbox": reg["bbox"], **lab})
    return out


def pairing(rows, page_id: str, bbox, gold: dict) -> dict | None:
    rel = load(ROOT / "fixtures" / "gold-relations.json")
    by_line = {g["id"]: g for g in gold["lines"]}
    match = None
    for t in rel["tables"]:
        if t["page"] != page_id:
            continue
        ids = [f"{page_id}-{g}" for row in t["rows"] for cell in row for g in cell]
        inside = sum(text_eval.membership([float(v) for v in by_line[i]["bbox"]], bbox) == "in" for i in ids if i in by_line)
        if ids and inside / len(ids) >= 0.5:
            match = t
    if match is None:
        return None
    out_rows = [" ".join(vlm_tasks.row_cells(r)) for r in (rows or [])]
    res = []
    for row in match["rows"]:
        cond = " ".join(by_line[f"{page_id}-{g}"]["text"] for g in row[0] if f"{page_id}-{g}" in by_line)
        val = " ".join(by_line[f"{page_id}-{g}"]["text"] for cell in row[1:] for g in cell if f"{page_id}-{g}" in by_line)
        ok = any(text_eval.norm(val) and text_eval.norm(val) in text_eval.norm(r)
                 and text_eval.similarity_cover(cond, r) >= 0.9 for r in out_rows)
        res.append({"condition": cond, "value": val, "paired": ok})
    return {"gold_table": match["id"], "rows": len(res), "paired": sum(x["paired"] for x in res)}


def main() -> int:
    out = Path(sys.argv[1])
    result = load(out / "result.json")
    reads = load(out / "trace" / "reads.json")
    labels = table_labels()
    regions_by_page: dict[str, list] = {}
    for r in result["regions"]:
        regions_by_page.setdefault(r["page_id"], []).append(r)
    un_by_page: dict[str, list] = {}
    for u in result["unassigned_texts"]:
        un_by_page.setdefault(u["page_id"], []).append(u)
    rows = {"general": [], "table": []}
    region_eval, page_eval, tables = {}, {}, []
    page_totals: Counter = Counter()
    for p in result["pages"]:
        # 후보 경계 실행(CANDIDATE_RUN) 페이지는 섞지 않는다(긴 이미지 평가는 별도 표).
        if p["execution_status"] != "READY":
            continue
        gold, problem = load_gold(p["page_id"], p["image_sha256"], [p["width"], p["height"]])
        if gold is None:
            continue
        regs = regions_by_page.get(p["page_id"], [])
        for r in regs:
            e = text_eval.evaluate_region(r["text"], r["bbox"], gold["lines"])
            region_eval[r["region_id"]] = e
            tr = reads.get(r["region_id"]) or {}
            rows["table" if r["table_candidate"] else "general"].append({
                "region_id": r["region_id"], "text": r["text"], "contract_ok": tr.get("contract_errors") == [],
                "uncertain": tr.get("uncertain"), "eval": e,
                "review": {"needs_review": r["needs_review"], "quality_reasons": r["review"]["quality"],
                           "context_reasons": r["review"]["context"]}})
            if r["table_candidate"]:
                lab = next((x for x in labels if x["page_id"] == r["page_id"] and iou(x["bbox"], r["bbox"]) >= 0.9), None)
                status = r["table"]["status"]
                tables.append({"region_id": r["region_id"], "status": status,
                               "label_is_table": None if lab is None else lab["is_table"],
                               "label_match": None if lab is None or status == "unknown" else (status == "table") == lab["is_table"],
                               "rows": len(r["table"]["rows"] or []),
                               "pairing": pairing(r["table"]["rows"], r["page_id"], r["bbox"], gold) if status == "table" else None})
        pe = text_eval.evaluate_page(gold["lines"], regs, un_by_page.get(p["page_id"], []))
        for g in pe["lines"]:
            g["bbox"] = next(x["bbox"] for x in gold["lines"] if x["id"] == g["gold_id"])
        page_eval[p["page_id"]] = pe
        for geo, c in pe["counts"].items():
            for k, v in c.items():
                page_totals[(geo, k)] += v
    agg = {k: eval_report.aggregate(v) for k, v in rows.items() if v}
    req = sum(page_totals.values())
    lines = ["# 통합 실행 평가", "", f"대상: 정답이 있는 READY 페이지 {len(page_eval)}개. " + eval_report.METRIC_NOTE, "",
             "## 페이지 단위(분모 = 페이지 정답 줄 전체, 선택 제외)", "",
             "| 정답 줄 기하 | 소속 영역 문구: 정확 | 근사 | 경계 걸친 영역 문구 | 다른 영역 문구 | 미배정 OCR 원문에만 | 판독 실패 영역 OCR 에만 | 누락 | 합계 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for geo in ("home", "no_home"):
        c = {k: v for (g, k), v in page_totals.items() if g == geo}
        lines.append(f"| {geo} | {c.get('home_exact', 0)} | {c.get('home_approx', 0)} | {c.get('boundary_region', 0)} | "
                     f"{c.get('other_region', 0)} | {c.get('unassigned_ocr_only', 0)} | {c.get('failed_region_ocr_only', 0)} | "
                     f"{c.get('missing', 0)} | {sum(c.values())} |")
    lines += ["", f"전체 정답 줄 {req}. home = 면적 50% 이상 포함하는 영역이 있음, no_home = 레이아웃 미검출 또는 일부 포함.", "",
              "## 영역 단위(eval/2)", "", eval_report.HEADER, eval_report.SEP]
    for k, a in agg.items():
        lines.append(eval_report.row_md(k, a))
    lines += ["", "검토 품질 근거: " + "; ".join(f"{k}: {a['review_quality_reasons']}" for k, a in agg.items()),
              "검토 맥락 근거: " + "; ".join(f"{k}: {a['review_context_reasons']}" for k, a in agg.items()), "",
              "## 표 후보", "",
              f"- 판정: {dict(Counter(t['status'] for t in tables))}, 라벨 일치 {sum(t['label_match'] is True for t in tables)}"
              f"/{sum(t['label_match'] is not None for t in tables)}, 불일치 {[t['region_id'] for t in tables if t['label_match'] is False]}, "
              f"판정 없음(unknown) {[t['region_id'] for t in tables if t['status'] == 'unknown']}",
              "- 조건-값 대응(정답 관계 파일에 있는 표만): " + "; ".join(
                  f"{t['region_id']}→{t['pairing']['gold_table']} {t['pairing']['paired']}/{t['pairing']['rows']}"
                  for t in tables if t["pairing"])]
    md = "\n".join(lines) + "\n"
    dump(out / "eval" / "eval.json", {"summary": agg, "page_totals": {f"{g}:{k}": v for (g, k), v in page_totals.items()},
                                      "tables": tables, "pages": page_eval, "regions": region_eval, "summary_markdown": md})
    (out / "eval" / "eval.md").write_text(md, encoding="utf-8")
    build_final_viewer(out, Path(result["settings"]["stage1"]))
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
