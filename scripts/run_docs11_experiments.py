"""docs/11 작은 비교(E1 배정 출처 표시, E2 순서 확인 재판독, E3 표 셀 구조). 기존 Stage 1·1차 응답을 재사용한다.

사용:
  run_docs11_experiments.py --exp E3 --out work/docs11-exp            # 호출 + 보고
  run_docs11_experiments.py --exp E1a,E1b,E2 --out work/docs11-exp
  run_docs11_experiments.py --report --out work/docs11-exp            # 저장 응답으로 보고만
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from layout_pipeline import page_read, table_cells, vlm_tasks  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.region_gold_eval import evaluate_table, evaluate_text  # noqa: E402
from layout_pipeline.structure_eval import evaluate_table_rows  # noqa: E402
from layout_pipeline.text_eval import membership  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402
from run_reread_experiment import context  # noqa: E402

NEW_S1 = ROOT / "work/pipeline/new-inputs-stage1-2026-10-26/stage1"
NEW_S1_INK = ROOT / "work/pipeline/new-inputs-stage1-ink-2026-10-26"
OLD_S1 = ROOT / "work/pipeline/page-first-v2-2026-10-26/stage1"
NEW_PF = ROOT / "work/pipeline/new-inputs-page-first-2026-10-26"
NEW_CROP = ROOT / "work/pipeline/new-inputs-crop-2026-10-26"
OLD_PF = ROOT / "work/pipeline/page-first-v2-2026-10-26"

# 사전 고정(docs/11 §1-3·§2-3·§3). 평가 전용 기대 구조는 fixtures/new-inputs-gold.json 과 아래 OLD_TABLE_GOLD.
E3_TARGETS = [("n02_p01-v0-L008", 3), ("n01_p01-t0-L000", 2), ("n02_p01-v0-L010", 2), ("n01_p01-t0-L001", 2),
              ("f01_p01-v0-L000", 2), ("f02_p01-v0-L002", 1)]
E2_TARGETS = ["n02_p01-v0-L004", "n02_p01-v0-L011", "n02_p01-v0-L020", "n02_p01-v0-L002", "f01_p01-v0-L004", "f01_p01-v0-L015"]
OLD_TABLE_GOLD = {"f01_p01-v0-L000": {"is_table": True, "n_rows": 5, "n_cols": 3, "cells": [
    {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "자금용도"},
    {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "상환방식"},
    {"row": 0, "col": 2, "rowspan": 1, "colspan": 1, "text": "대출기간"},
    {"row": 1, "col": 0, "rowspan": 2, "colspan": 1, "text": "운전자금"},
    {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "일시상환/혼합상환/마이너스통장"},
    {"row": 1, "col": 2, "rowspan": 1, "colspan": 1, "text": "1년 이내"},
    {"row": 2, "col": 1, "rowspan": 1, "colspan": 1, "text": "할부상환"},
    {"row": 2, "col": 2, "rowspan": 1, "colspan": 1, "text": "5년 이내"},
    {"row": 3, "col": 0, "rowspan": 2, "colspan": 1, "text": "시설자금"},
    {"row": 3, "col": 1, "rowspan": 1, "colspan": 1, "text": "일시상환/혼합상환"},
    {"row": 3, "col": 2, "rowspan": 1, "colspan": 1, "text": "3년 이내"},
    {"row": 4, "col": 1, "rowspan": 1, "colspan": 1, "text": "할부상환"},
    {"row": 4, "col": 2, "rowspan": 1, "colspan": 1, "text": "신용(5년 이내)/담보(20년 이내)"}],
    "relations": [{"id": "R1", "check": "span", "text": "운전자금", "row": 1, "col": 0, "rowspan": 2, "colspan": 1},
                  {"id": "R2", "check": "span", "text": "시설자금", "row": 3, "col": 0, "rowspan": 2, "colspan": 1}]}}


def stage1_for(rid: str, ink: bool = False) -> Path:
    if rid.startswith("n0"):
        return NEW_S1_INK if ink else NEW_S1
    return OLD_S1


def rec_region(rid: str, ink: bool = False):
    page = rid.split("-")[0]
    rec = load(stage1_for(rid, ink) / "records" / f"{page}.json")
    return rec, next(r for r in rec["regions"] if r["region_id"] == rid)


def jobs(exp: str) -> list[dict]:
    out = []
    if exp == "E3":
        for rid, reps in E3_TARGETS:
            for rep in range(1, reps + 1):
                out.append({"exp": exp, "cond": "cells", "rep": rep, "key": rid, "rid": rid})
    elif exp == "E2":
        for rid in E2_TARGETS:
            for rep in (1, 2):
                out.append({"exp": exp, "cond": "B", "rep": rep, "key": rid, "rid": rid})
    elif exp == "E1a":
        for cond in ("strict", "ink"):
            for rep in (1, 2):
                out.append({"exp": exp, "cond": cond, "rep": rep, "key": "n02_p01-v0-L004", "rid": "n02_p01-v0-L004"})
    elif exp == "E1b":
        for cond in ("strict", "ink"):
            for rep in (1, 2):
                out.append({"exp": exp, "cond": cond, "rep": rep, "key": "crop-vlm-0002", "batch": "vlm-0002"})
    return out


def build(job: dict) -> tuple[dict, dict]:
    if job["exp"] == "E3":
        rec, reg = rec_region(job["rid"])
        img = Image.open(rec["page"]["image_path"]).convert("RGB")
        view = table_cells.context_view_for(rec, reg)
        req = table_cells.request(rec, reg, img, view)
        return req, {"kind": "table_cells", "max_tokens": table_cells.MAX_TOKENS, "name": "table_cells_read",
                     "context_view": view}
    if job["exp"] in ("E2", "E1a"):
        ink = job["cond"] == "ink"
        rec, reg = rec_region(job["rid"], ink)
        img = Image.open(rec["page"]["image_path"]).convert("RGB")
        run = NEW_PF if job["rid"].startswith("n0") else OLD_PF
        first, view = context(run, job["rid"], load(run / "trace" / "reads.json"))
        req = page_read.reread_request(rec, reg, img, first, view)
        return req, {"kind": "reread", "max_tokens": page_read.REREAD_MAX_TOKENS, "name": "region_reread",
                     "context_view": view, "first_text": first}
    # E1b: 저장된 crop/1 묶음 요청을 같은 영역·순서로 재현하고 후보 출처 표시만 바꾼다.
    stored = load(NEW_CROP / "trace" / "vlm" / f"{job['batch']}.json")
    rids = stored["request_meta"]["region_ids"]
    rec = load((NEW_S1_INK if job["cond"] == "ink" else NEW_S1) / "records" / f"{rids[0].split('-')[0]}.json")
    regs = [next(r for r in rec["regions"] if r["region_id"] == i) for i in rids]
    img = Image.open(rec["page"]["image_path"]).convert("RGB")
    req = vlm_tasks.crop_request(rec, regs, img, 0, "crop/1")
    if job["cond"] == "strict" and req["text"] != stored["request_meta"]["prompt"]:
        raise RuntimeError("E1b strict 재현 요청이 저장 요청과 다르다")
    return req, {"kind": "general", "max_tokens": 8000, "name": "general_read"}


def run(a) -> None:
    load_env_file()
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"],
                       a.timeout or float(os.environ.get("PN_VLM_TIMEOUT_S", "320")), prefix="d11")
    todo = [j for e in a.exp.split(",") for j in jobs(e)]
    done = {f.stem for f in (a.out / "calls").glob("*.json")}
    todo = [j for j in todo if f"{j['exp']}-{j['cond']}-r{j['rep']}-{j['key']}" not in done]     # 이어서 실행

    def one(job):
        req, how = build(job)
        t0 = time.perf_counter()
        call = client.chat(req["text"], req["images"], req["schema"], how["name"], how["max_tokens"],
                           {"kind": how["kind"], "exp": job["exp"], "cond": job["cond"], "rep": job["rep"],
                            "region_ids": req["region_ids"], "version": req.get("version"), "items": req.get("items"),
                            "image_roles": req.get("image_roles"), "context_view": how.get("context_view"),
                            "first_text": how.get("first_text")})
        rec = asdict(call) | {"wall_seconds": round(time.perf_counter() - t0, 2)}
        dump(a.out / "calls" / f"{job['exp']}-{job['cond']}-r{job['rep']}-{job['key']}.json", rec)
        print(job["exp"], job["cond"], job["rep"], job["key"], call.finish_reason, call.error, rec["wall_seconds"], flush=True)

    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        list(pool.map(one, todo))


def gold_lines_order(rid: str) -> list[str]:
    """이전 페이지 정답 줄(fixtures/gold) 중 영역 안 줄을 위→아래로(순서 확인 전용)."""
    page = rid.split("-")[0]
    rec, reg = rec_region(rid)
    g = load(ROOT / "fixtures" / "gold" / f"{page}.json")
    return [x["text"] for x in sorted((x for x in g["lines"] if membership(x["bbox"], reg["bbox"]) == "in" and not x.get("optional")),
                                     key=lambda x: (x["bbox"][1], x["bbox"][0]))]


def report(a) -> None:
    gold = load(ROOT / "fixtures" / "new-inputs-gold.json")["regions"]
    sg = load(ROOT / "fixtures" / "structure-gold.json")
    rows_out, md = [], ["# docs/11 작은 비교 결과", "", "계획·기준: docs/11. 정답: fixtures/new-inputs-gold.json(AI 작성), fixtures/gold·structure-gold(이전).", ""]
    for f in sorted((a.out / "calls").glob("*.json")):
        c = load(f)
        m = c["request_meta"]
        exp, cond, rep = m["exp"], m["cond"], m["rep"]
        call = SimpleNamespace(error=c["error"], parsed=c["parsed"])
        if exp == "E3":
            rid = m["region_ids"][0]
            p = table_cells.validate(call, {"region_ids": [rid]})[rid]
            table = {"status": "table" if p["is_table"] is True else ("not_table" if p["is_table"] is False else "unknown"),
                     "rows": p["rows"], "cells": p["cells"], "n_rows": p["n_rows"], "n_cols": p["n_cols"]}
            gt = (gold.get(rid) or {}).get("table") or OLD_TABLE_GOLD.get(rid)
            ev = evaluate_table(gt, table) if gt else {"status": table["status"], "ok": table["status"] == "table"}
            if rid in sg["table_rows"]:
                page = rid.split("-")[0]
                gby = {x["id"]: x for x in load(ROOT / "fixtures" / "gold" / f"{page}.json")["lines"]}
                tr = evaluate_table_rows(p["rows"], [[f"{page}-{x}" for x in r] for r in sg["table_rows"][rid]["rows"]], gby)
                ev = (ev or {}) | {"old_rows_kept": f"{tr['kept']}/{tr['rows']}"}
            te = evaluate_text(gold[rid], p["text"]) if rid in gold else None
            rows_out.append({"exp": exp, "cond": cond, "rep": rep, "region_id": rid, "contract_ok": p["contract_ok"],
                             "errors": p["contract_errors"], "table_eval": ev, "text_eval": te, "seconds": c["seconds"],
                             "cells": p["cells"], "text": p["text"], "notes": p["notes"]})
        elif exp in ("E2", "E1a"):
            rid = m["region_ids"][0]
            p = page_read.validate_reread(call, {"region_ids": [rid]})[rid]
            rec, reg = rec_region(rid, cond == "ink")
            sel = page_read.select_reread(rec, reg, m.get("first_text"), p)
            if rid in gold:
                te = evaluate_text(gold[rid], sel["text"])
            else:
                te = evaluate_text({"lines": gold_lines_order(rid)}, sel["text"])
            rows_out.append({"exp": exp, "cond": cond, "rep": rep, "region_id": rid, "contract_ok": p["contract_ok"],
                             "source": sel["source"], "review": sel["review"], "text": sel["text"], "first_text": m.get("first_text"),
                             "order_conflict_after": page_read.order_conflict(rec, reg, sel["text"] or ""),
                             "text_eval": te, "seconds": c["seconds"], "changes": p.get("changes")})
        else:
            parsed = vlm_tasks.validate(call, {"mode": "crop", "region_ids": m["region_ids"]})
            for rid in m["region_ids"]:
                te = evaluate_text(gold[rid], parsed[rid]["text"]) if rid in gold else None
                rows_out.append({"exp": exp, "cond": cond, "rep": rep, "region_id": rid, "contract_ok": parsed[rid]["contract_ok"],
                                 "text": parsed[rid]["text"], "text_eval": te, "seconds": c["seconds"]})
    dump(a.out / "report.json", rows_out)

    def te_s(te):
        if not te:
            return "-"
        bits = [f"누락{len(te['missing_lines'])}" if te["missing_lines"] else "", "순서✗" if not te["order_ok"] else "",
                f"글자{te['char_errors']}" if te["char_errors"] else "", ("기호 " + "".join(te["stray_chars"])) if te["stray_chars"] else "",
                ("추가 " + ",".join(te["added_segments"])) if te["added_segments"] else ""]
        return " ".join(b for b in bits if b) or "정상"
    for exp in ("E3", "E2", "E1a", "E1b"):
        rs = [r for r in rows_out if r["exp"] == exp]
        if not rs:
            continue
        md += [f"## {exp}", ""]
        if exp == "E3":
            md += ["| 영역 | 회 | 계약 | 표 판정 | 셀 일치 | 행 혼합 | 관계 | 이전 행 보존 | 문구 | 초 |", "|---|---:|---|---|---|---|---|---|---|---:|"]
            for r in rs:
                ev = r["table_eval"] or {}
                md.append(f"| {r['region_id']} | {r['rep']} | {'통과' if r['contract_ok'] else '실패 ' + '; '.join(r['errors'])} | "
                          f"{ev.get('status')}{' ✓' if ev.get('ok') else ' ✗'} | {ev.get('cells_exact', '-')}/{ev.get('cells_gold', '-')} | "
                          f"{ev.get('rows_mixed', '-')} | {ev.get('relations', '-')} | {ev.get('old_rows_kept', '-')} | {te_s(r['text_eval'])} | {r['seconds']} |")
        elif exp in ("E2", "E1a"):
            md += ["| 영역 | 조건 | 회 | 선택 | 순서 조건(재판독 후) | 정답 대조 | 초 |", "|---|---|---:|---|---|---|---:|"]
            for r in rs:
                md.append(f"| {r['region_id']} | {r['cond']} | {r['rep']} | {r['source']} {','.join(r['review'])} | "
                          f"{'남음' if r['order_conflict_after'] else '없음'} | {te_s(r['text_eval'])} | {r['seconds']} |")
        else:
            md += ["| 영역 | 조건 | 회 | 정답 대조 | 초(묶음) |", "|---|---|---:|---|---:|"]
            for r in rs:
                md.append(f"| {r['region_id']} | {r['cond']} | {r['rep']} | {te_s(r['text_eval'])} | {r['seconds']} |")
        md.append("")
    (a.out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--timeout", type=float, default=None, help="응답 대기 한도(초). 기본 PN_VLM_TIMEOUT_S")
    a = ap.parse_args()
    a.out = ROOT / a.out if not a.out.is_absolute() else a.out
    if a.exp:
        run(a)
    report(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
