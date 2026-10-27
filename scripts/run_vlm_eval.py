"""VLM 판독 실험: 일반 영역 A(원래 페이지 배치) / B(검출 영역 전체 crop) 비교와 OCR 표 후보 확인.

같은 Stage 1 실행(영역·OCR 초안 고정)과 같은 평가 목록을 쓴다. 동시성 1(비교 조건 고정).
계약 통과와 내용 품질을 따로 집계한다. 결과·원시 응답·보낸 이미지·프롬프트를 모두 남긴다.

사용: run_vlm_eval.py --run work/runs/stage1-merge-large --set fixtures/vlm-eval-set.json --out work/vlm/<name>
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import eval_report, text_eval, vlm_tasks  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.gold_eval import load_gold  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402


def save_inputs(out: Path, call_id: str, request: dict, page: dict) -> list[str]:
    paths = []
    for k, (im, role) in enumerate(zip(request["images"], request["image_roles"])):
        if role == "page_original":
            paths.append(page["image_path"])     # 페이지 PNG 원본 그대로(재인코딩 바이트는 calls 의 sha 로 기록)
            continue
        p = out / "inputs" / f"{call_id}-{k}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        im.save(p)
        paths.append(str(p))
    return paths


def run_call(client, out: Path, request: dict, page: dict, max_tokens: int) -> tuple[dict, dict]:
    call = client.chat(request["text"], request["images"], request["schema"], f"{request['mode']}_read", max_tokens,
                       {"mode": request["mode"], "page_id": page["page_id"], "region_ids": request["region_ids"],
                        "image_roles": request["image_roles"], "version": request.get("version"),
                        "margin_px": request.get("margin_px"), "items": request.get("items")})
    rec = asdict(call)
    rec["input_paths"] = save_inputs(out, call.call_id, request, page)
    dump(out / "calls" / f"{call.call_id}.json", rec)
    print(f"{call.call_id} {request['mode']} {page['page_id']} n={len(request['region_ids'])} "
          f"{call.seconds}s finish={call.finish_reason} err={call.error}", flush=True)
    return rec, vlm_tasks.validate(call, request)


def pairing(rows: list | None, page_id: str, bbox, gold: dict | None) -> dict | None:
    """정답 관계 파일에 있는 표 중 이 후보 bbox 안에 정답 줄 과반이 들어가는 표만: 조건과 값이 같은 출력 행에 있는지."""
    if gold is None:
        return None
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
    return {"gold_table": match["id"], "rows": len(res), "paired": sum(x["paired"] for x in res),
            "output_rows": len(out_rows), "detail": res}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--set", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--modes", default="page,crop,table")
    ap.add_argument("--max-images", type=int, default=8, help="B 방식 요청당 영역 crop 수")
    ap.add_argument("--crop-margin", choices=["none", "ocr-median-0.3"], default="none",
                    help="B 방식 crop 여백 규칙(저장 bbox 불변, 모델 입력만)")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--reeval", action="store_true", help="저장된 결과로 평가·검토 사유·화면만 다시 계산(호출 없음)")
    args = ap.parse_args()
    if args.reeval:
        return reevaluate(args.out, args.run)
    if args.out.exists() and any(args.out.iterdir()):
        ap.error(f"출력 폴더가 비어 있지 않습니다: {args.out}")
    load_env_file()
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")))
    spec = load(args.set)
    modes = args.modes.split(",")
    results: dict[str, list] = {"page": [], "crop": [], "table": []}
    calls_meta = []
    duplicates = {}
    for page_id, entry in spec["general"].items():
        record = load(args.run / "records" / f"{page_id}.json")
        page = record["page"]
        img = Image.open(page["image_path"]).convert("RGB")
        regions = {r["region_id"]: r for r in record["regions"]}
        targets = [regions[i] for i in entry["regions"]]
        gold, _ = load_gold(page_id, page["image_sha256"], [page["width"], page["height"]])
        chunks = {"page": [targets], "crop": [targets[i:i + args.max_images] for i in range(0, len(targets), args.max_images)]}
        for mode in ("page", "crop"):
            if mode not in modes:
                continue
            texts = {}
            for chunk in chunks[mode]:
                if mode == "page":
                    req = vlm_tasks.page_request(record, chunk, img)
                else:
                    margin = vlm_tasks.page_margin(record) if args.crop_margin != "none" else 0
                    req = vlm_tasks.crop_request(record, chunk, img, margin)
                rec, parsed = run_call(client, args.out, req, page, args.max_tokens)
                calls_meta.append({k: rec[k] for k in ("call_id", "seconds", "finish_reason", "usage", "error")}
                                  | {"mode": mode, "page_id": page_id, "regions": len(chunk), "images": rec["images"]})
                for r in chunk:
                    res = parsed[r["region_id"]]
                    texts[r["region_id"]] = res["text"]
                    results[mode].append({"page_id": page_id, "region_id": r["region_id"], "call_id": rec["call_id"],
                                          "bbox": r["bbox"], "raw_label": r["raw_label"], **res,
                                          "review": vlm_tasks.review_reasons(record, r, res),
                                          "eval": text_eval.evaluate_region(res["text"], r["bbox"], gold["lines"]) if gold else None})
            duplicates[f"{mode}:{page_id}"] = vlm_tasks.cross_region_duplicates(texts, record["diagnostics"]["region_overlaps"])
    if "table" in modes:
        labels = spec["table_labels"]["labels"]
        for rec_path in sorted((args.run / "records").glob("*.json")):
            record = load(rec_path)
            if record["status"] != "READY":
                continue
            page = record["page"]
            cands = [r for r in record["regions"] if r["table_candidate"]]
            if not cands:
                continue
            img = Image.open(page["image_path"]).convert("RGB")
            gold, _ = load_gold(page["page_id"], page["image_sha256"], [page["width"], page["height"]])
            for r in cands:
                req = vlm_tasks.table_request(record, r, img)
                rec, parsed = run_call(client, args.out, req, page, 4000)
                calls_meta.append({k: rec[k] for k in ("call_id", "seconds", "finish_reason", "usage", "error")}
                                  | {"mode": "table", "page_id": page["page_id"], "regions": 1, "images": rec["images"]})
                res = parsed[r["region_id"]]
                label = labels.get(r["region_id"])
                results["table"].append({
                    "page_id": page["page_id"], "region_id": r["region_id"], "call_id": rec["call_id"], "bbox": r["bbox"],
                    **res, "label": label,
                    "is_table_correct": None if label is None or res.get("is_table") is None else res["is_table"] == label["is_table"],
                    "review": vlm_tasks.review_reasons(record, r, res),
                    "eval": text_eval.evaluate_region(res["text"], r["bbox"], gold["lines"]) if gold else None,
                    "pairing": pairing(res.get("rows"), page["page_id"], r["bbox"], gold) if res.get("is_table") else None})
    summary = summarize(results, calls_meta, duplicates)
    dump(args.out / "results.json", {"run": str(args.run), "set": str(args.set), "results": results,
                                      "cross_region_duplicates": duplicates, "calls": calls_meta, "summary": summary})
    (args.out / "summary.md").write_text(summary["markdown"], encoding="utf-8")
    build_viewer(args.out, results, summary)
    print(summary["markdown"])
    return 0


def reevaluate(out: Path, run: Path) -> int:
    """저장된 VLM 응답(결과 필드)은 그대로 두고 평가·검토 사유·요약·화면만 다시 계산한다."""
    saved = load(out / "results.json")
    cache = {}
    for mode, rows in saved["results"].items():
        for row in rows:
            pid = row["page_id"]
            if pid not in cache:
                record = load(run / "records" / f"{pid}.json")
                page = record["page"]
                gold, _ = load_gold(pid, page["image_sha256"], [page["width"], page["height"]])
                cache[pid] = (record, {r["region_id"]: r for r in record["regions"]}, gold)
            record, regions, gold = cache[pid]
            region = regions[row["region_id"]]
            row["review"] = vlm_tasks.review_reasons(record, region, row)
            row["eval"] = text_eval.evaluate_region(row["text"], region["bbox"], gold["lines"]) if gold else None
            if mode == "table":
                row["pairing"] = pairing(row.get("rows"), pid, region["bbox"], gold) if row.get("is_table") else None
    dups = {}
    for mode in ("page", "crop"):
        for pid in sorted({r["page_id"] for r in saved["results"].get(mode, [])}):
            texts = {r["region_id"]: r["text"] for r in saved["results"][mode] if r["page_id"] == pid}
            dups[f"{mode}:{pid}"] = vlm_tasks.cross_region_duplicates(texts, cache[pid][0]["diagnostics"]["region_overlaps"])
    saved["cross_region_duplicates"] = dups
    summary = summarize(saved["results"], saved["calls"], dups)
    saved["summary"] = summary
    saved["reevaluated"] = True
    dump(out / "results.json", saved)
    (out / "summary.md").write_text(summary["markdown"], encoding="utf-8")
    build_viewer(out, saved["results"], summary)
    print(summary["markdown"])
    return 0


def agg(rows: list[dict]) -> dict:
    return eval_report.aggregate(rows)


def summarize(results: dict, calls: list[dict], duplicates: dict) -> dict:
    out = {"modes": {}, "calls": {}}
    for mode, rows in results.items():
        if rows:
            out["modes"][mode] = agg(rows)
            cs = [c for c in calls if c["mode"] == mode]
            out["calls"][mode] = {"http_calls": len(cs), "seconds": round(sum(c["seconds"] for c in cs), 2),
                                  "finish_not_stop": sum(c["finish_reason"] != "stop" for c in cs),
                                  "errors": sum(bool(c["error"]) for c in cs),
                                  "prompt_tokens": sum((c["usage"] or {}).get("prompt_tokens", 0) for c in cs),
                                  "completion_tokens": sum((c["usage"] or {}).get("completion_tokens", 0) for c in cs)}
    t = results.get("table") or []
    if t:
        out["table"] = {"candidates": len(t), "is_table_true": sum(r.get("is_table") is True for r in t),
                        "is_table_false": sum(r.get("is_table") is False for r in t),
                        "is_table_correct": sum(r["is_table_correct"] is True for r in t),
                        "is_table_wrong": [r["region_id"] for r in t if r["is_table_correct"] is False],
                        "false_kept_text": [r["region_id"] for r in t if r.get("is_table") is False and r["text"]],
                        "pairing": [{"region_id": r["region_id"], **{k: r["pairing"][k] for k in ("gold_table", "rows", "paired")}}
                                    for r in t if r.get("pairing")]}
    out["cross_region_duplicates"] = {k: {"lines": len(v), "from_overlapping_regions": sum(x["regions_overlap"] for x in v)}
                                      for k, v in duplicates.items() if v}
    lines = ["# VLM 판독 실험 요약", "", "계약 통과(HTTP/finish/JSON/ID/타입)와 내용 품질(정답 대비)은 별도 지표다. "
             "정답 소속 기준: eval-membership/1(정답 줄 면적 50% 이상이 영역 안). " + eval_report.METRIC_NOTE, "",
             eval_report.HEADER, eval_report.SEP]
    for mode, a in out["modes"].items():
        lines.append(eval_report.row_md(mode, a))
    lines += ["", "| 조건 | HTTP 호출 | 초 | 입력/출력 토큰 | finish≠stop |", "|---|---:|---:|---|---:|"]
    for mode, c in out["calls"].items():
        lines.append(f"| {mode} | {c['http_calls']} | {c['seconds']} | {c['prompt_tokens']}/{c['completion_tokens']} | {c['finish_not_stop']} |")
    lines += ["", "검토 품질 근거: " + "; ".join(f"{m}: {a['review_quality_reasons']}" for m, a in out["modes"].items()),
              "검토 맥락 근거: " + "; ".join(f"{m}: {a['review_context_reasons']}" for m, a in out["modes"].items())]
    if "table" in out:
        tb = out["table"]
        lines += ["", "## 표 후보", "", f"- 후보 {tb['candidates']}개: is_table=true {tb['is_table_true']}, false {tb['is_table_false']}, "
                  f"라벨과 일치 {tb['is_table_correct']}, 불일치 {tb['is_table_wrong']}",
                  f"- false 판정이면서 문구 보존: {tb['false_kept_text']}",
                  "- 조건-값 대응(정답 관계 파일에 있는 표만): " + "; ".join(f"{p['region_id']}→{p['gold_table']} {p['paired']}/{p['rows']}" for p in tb["pairing"])]
    lines += ["", "## 서로 다른 Region 사이 반복 줄(측정만, 삭제하지 않음)", "",
              "; ".join(f"{k}: {v['lines']}줄(원시 중첩 영역 {v['from_overlapping_regions']})"
                        for k, v in out["cross_region_duplicates"].items()) or "없음"]
    out["markdown"] = "\n".join(lines) + "\n"
    return out


def build_viewer(out: Path, results: dict, summary: dict) -> None:
    """영역별 원본 crop, 정답, OCR 초안, A/B 결과, 표 후보 판정을 나란히 보는 정적 HTML."""
    crops = out / "viewer" / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    by_region: dict[str, dict] = {}
    for mode in ("page", "crop"):
        for r in results[mode]:
            by_region.setdefault(r["region_id"], {})[mode] = r
    esc = lambda s: html.escape(s if isinstance(s, str) else ("null" if s is None else str(s)))  # noqa: E731
    parts = ["<!doctype html><html lang=ko><head><meta charset=utf-8><title>VLM 판독 평가</title><style>"
             "body{font:13px/1.45 'Malgun Gothic',sans-serif;margin:16px;background:#f6f7f9;color:#1d232b}"
             ".card{background:#fff;border:1px solid #d8dde3;margin:0 0 14px;padding:10px}"
             ".grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}pre{white-space:pre-wrap;margin:0;"
             "background:#f4f5f7;padding:6px;font:12px/1.4 'Malgun Gothic',sans-serif}img{max-width:100%;border:1px solid #ccc}"
             ".bad{color:#c62828}.mute{color:#5c6773}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:2px 5px}"
             "h3{margin:0 0 6px;font-size:14px}</style></head><body><h1>VLM 판독 평가</h1>",
             f"<pre>{esc(summary['markdown'])}</pre>"]
    from PIL import Image as _I
    for rid, modes in by_region.items():
        any_r = next(iter(modes.values()))
        img_path = crops / f"{rid}.png"
        src = None   # 원본 crop 은 B 방식에 실제로 보낸 입력 저장본을 쓴다
        cr = modes.get("crop")
        if cr:
            call = load(out / "calls" / f"{cr['call_id']}.json")
            idx = call["request_meta"]["region_ids"].index(rid)
            src = call["input_paths"][idx]
        if src:
            _I.open(src).save(img_path)
        ev = (modes.get("page") or modes.get("crop"))["eval"] or {}
        cells = [f"<div><h3>원본 영역 crop</h3>{'<img src=crops/' + rid + '.png>' if src else ''}</div>",
                 f"<div><h3>정답(기대 줄 {ev.get('expected_lines')})</h3><pre>{esc(ev.get('expected_text'))}</pre>"
                 f"<h3>OCR 참고(배정+경계≥0.5)</h3><pre>{esc(any_r['review']['ocr_reference'])}</pre></div>"]
        for mode, name in (("page", "A 원래 페이지 배치"), ("crop", "B 영역 crop")):
            r = modes.get(mode)
            if not r:
                cells.append("<div></div>")
                continue
            e = r["eval"] or {}
            rv = r["review"]
            cells.append(f"<div><h3>{name}</h3><pre>{esc(r['text'])}</pre><div class=mute>정확 {e.get('lines_exact')} · 근사 {e.get('lines_approx_only')} · 누락 {len(e.get('missing_lines', []))} / {e.get('expected_lines')} "
                         f"· 근거 없는 글자 {e.get('unsupported_chars')} · 추가 구간 {esc(e.get('added_segments'))} · 위치 오류 {e.get('location_error_chars')} · 숫자 누락 {e.get('missing_digits')} "
                         f"추가 {e.get('extra_digits')} · 중복 {e.get('duplicated_lines')}</div>"
                         f"<div class={'bad' if rv['quality_reasons'] else 'mute'}>검토: {', '.join(rv['quality_reasons'] + rv['context_reasons']) or '없음'}"
                         f" {esc(rv['measures']) if rv['measures'] else ''}</div>"
                         f"<div class=mute>계약 {'통과' if r['contract_ok'] else esc(r['contract_errors'])} · uncertain={r['uncertain']}</div></div>")
        parts.append(f"<div class=card id='{rid}'><b>{rid}</b> <span class=mute>{esc(any_r['raw_label'])} bbox {[round(v) for v in any_r['bbox']]}</span>"
                     f"<div class=grid>{''.join(cells)}</div></div>")
    if results["table"]:
        parts.append("<h2>OCR 표 후보</h2>")
        for r in results["table"]:
            call = load(out / "calls" / f"{r['call_id']}.json")
            _I.open(call["input_paths"][0]).save(crops / f"T-{r['region_id']}.png")
            rows = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in vlm_tasks.row_cells(row)) + "</tr>" for row in (r.get("rows") or []))
            e = r["eval"] or {}
            pr = r.get("pairing")
            parts.append(f"<div class=card id='T-{r['region_id']}'><b>{r['region_id']}</b> 라벨 is_table={r['label']['is_table'] if r['label'] else '-'}"
                         f" · VLM is_table={r.get('is_table')} <span class={'bad' if r['is_table_correct'] is False else 'mute'}>"
                         f"({'일치' if r['is_table_correct'] else '불일치' if r['is_table_correct'] is False else '-'})</span>"
                         f" · 정확 {e.get('lines_exact')} 근사 {e.get('lines_approx_only')} 누락 {len(e.get('missing_lines', []))}/{e.get('expected_lines')} · 근거 없는 글자 {e.get('unsupported_chars')} 추가 {esc(e.get('added_segments'))}"
                         f"{' · 조건-값 ' + str(pr['paired']) + '/' + str(pr['rows']) if pr else ''}"
                         f"<div class=grid style='grid-template-columns:1fr 1fr 1fr'><div><img src=crops/T-{r['region_id']}.png></div>"
                         f"<div><pre>{esc(r['text'])}</pre></div><div><table>{rows}</table></div></div></div>")
    parts.append("</body></html>")
    (out / "viewer" / "index.html").write_text("".join(parts), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
