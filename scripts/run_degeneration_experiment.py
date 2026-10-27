"""docs/13: 1차 페이지 판독 반복 퇴화 실험. 기존 실행 폴더는 읽기만 하고 결과는 --out 에 쓴다.

  capability : 서버가 JSON 스키마 maxLength 를 강제하는지, frequency_penalty 를 받는지(이미지 없는 짧은 요청)
  run        : 실패 사례·대조 판독 단위를 Stage 1 기록으로 다시 만들어(원래 호출과 프롬프트·이미지 해시 확인) 조건별로 보낸다
               base(현재 요청, 출력 한도만 --max-tokens) | I1(영역 text maxLength + 예상량 출력 한도) | I2(frequency_penalty)
  summarize  : 조건별 결과와 대조(정답·실제 반복 문구·재판독 조건) 집계. 대조의 기준값은 원래 실행의 1차 응답(호출 0)

사용:
  run_degeneration_experiment.py capability --out work/degeneration/exp1
  run_degeneration_experiment.py run --out work/degeneration/exp1 --condition base --views fail --reps 3 --max-tokens 6000
  run_degeneration_experiment.py summarize --out work/degeneration/exp1
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import page_read, text_eval  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.vlm_client import VlmClient, png_part  # noqa: E402

RUN = ROOT / "work" / "pipeline" / "tmp-inputs-2026-10-27-r1"
FAIL_VIEWS = ["t01_p01-v0-b1", "t07_p08-v0", "t08_p02-v0"]
CONTROL_VIEWS = ["t01_p01-v0-b0", "t02_p01-v0", "t07_p07-v0", "t05_p01-t4"]
LEGIT_REPEATS = {"t01_p01-v0-b0": ["300만원이익", "210만원"]}     # 원본에 실제로 반복되는 문구(정규화)
FREQ_PENALTY = 0.3


def client(extra: dict | None = None) -> VlmClient:
    load_env_file()

    def post(url, json, timeout):          # noqa: A002 - requests.post 와 같은 인자 이름
        return requests.post(url, json=json | (extra or {}), timeout=timeout)
    return VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                     post=post, prefix="dgn")


# ---- 1. 서버 기능 확인 ----
def capability(out: Path) -> None:
    long = "퇴직연금 수수료는 제도와 가입채널에 따라 차등 적용됩니다. " * 3
    rows = {}
    schema = {"type": "object", "additionalProperties": False, "required": ["text"],
              "properties": {"text": {"type": ["string", "null"], "maxLength": 20}}}
    c = client().chat(f"다음 문장을 text 에 그대로 쓰세요: {long}", [], schema, "cap_maxlen", 300)
    rows["maxLength_20"] = {"finish": c.finish_reason, "error": c.error, "text": (c.parsed or {}).get("text"),
                            "len": len((c.parsed or {}).get("text") or ""), "enforced": len((c.parsed or {}).get("text") or "") <= 20}
    schema2 = {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {"text": {"type": "string"}}}
    for name, extra in (("frequency_penalty_0.3", {"frequency_penalty": FREQ_PENALTY}),):
        c2 = client(extra).chat("'가나다' 를 공백으로 구분해 30번 반복해 text 에 쓰세요.", [], schema2, "cap_fp", 300)
        rows[name] = {"http": c2.http_status, "finish": c2.finish_reason, "error": c2.error,
                      "repeats": ((c2.parsed or {}).get("text") or "").count("가나다")}
    c3 = client().chat("'가나다' 를 공백으로 구분해 30번 반복해 text 에 쓰세요.", [], schema2, "cap_fp0", 300)
    rows["frequency_penalty_none"] = {"http": c3.http_status, "finish": c3.finish_reason, "error": c3.error,
                                      "repeats": ((c3.parsed or {}).get("text") or "").count("가나다")}
    dump(out / "capability.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=1))


# ---- 2·3. 판독 단위 재구성과 조건 ----
def original_calls() -> dict[str, dict]:
    out = {}
    for p in (RUN / "trace" / "vlm").glob("*.json"):
        c = load(p)
        if c["request_meta"].get("kind") == "page":
            out[c["request_meta"]["view_id"]] = c
    return out


def rebuild(view_id: str) -> tuple[dict, dict, list, Image.Image]:
    page_id = view_id.split("-")[0]
    record = load(RUN / "stage1" / "records" / f"{page_id}.json")
    img = Image.open(ROOT / record["page"]["image_path"]).convert("RGB")
    for view in record["route"]["views"]:
        for bview, bregs in page_read.band_views(view, page_read.view_regions(record, view)):
            if bview["view_id"] == view_id:
                return record, bview, bregs, img
    raise KeyError(view_id)


def region_caps(record: dict, regions: list[dict]) -> dict[str, int]:
    return {r["region_id"]: max(300, 3 * sum(len(o["ocr_text"]) for o in page_read.region_ocr(record, r)) + 200) for r in regions}


def view_budget(record: dict, regions: list[dict]) -> int:
    return min(12000, 3 * sum(len(o["ocr_text"]) for r in regions for o in page_read.region_ocr(record, r)) + 1000)


def run(out: Path, condition: str, views: list[str], reps: int, max_tokens: int, concurrency: int) -> None:
    orig = original_calls()
    extra = {"frequency_penalty": FREQ_PENALTY} if condition == "I2" else None
    cl = client(extra)
    jobs = []
    for vid in views:
        record, view, regions, img = rebuild(vid)
        req = page_read.page_request(record, view, img, True, regions)
        o = orig[vid]
        same_prompt = req["text"] == o["request_meta"]["prompt"]
        same_images = [png_part(im)[1]["sha256"] for im in req["images"]] == [m["sha256"] for m in o["images"]]
        if not (same_prompt and same_images):
            raise SystemExit(f"{vid}: 원래 호출과 요청이 다르다(prompt {same_prompt}, images {same_images})")
        caps = region_caps(record, regions)
        schema, budget = req["schema"], max_tokens
        if condition == "I1":
            schema = copy.deepcopy(req["schema"])
            schema["properties"]["regions"]["items"]["properties"]["text"]["maxLength"] = max(caps.values())
            budget = view_budget(record, regions)
        for k in range(reps):
            jobs.append((vid, k, req, schema, budget, caps))

    def send(job):
        vid, k, req, schema, budget, caps = job
        call = cl.chat(req["text"], req["images"], schema, "page_read", budget,
                       {"kind": "page", "view_id": vid, "condition": condition, "rep": k, "region_ids": req["region_ids"],
                        "table_ids": req["table_ids"], "caps": caps})
        rec = asdict(call)
        dump(out / "calls" / condition / f"{vid}-r{k}.json", rec)
        return vid, k, rec
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for vid, k, rec in pool.map(send, jobs):
            print(condition, vid, k, rec["finish_reason"], (rec.get("usage") or {}).get("completion_tokens"), rec["seconds"], flush=True)


# ---- 집계 ----
def gold_for(page: dict):
    sha2gold = {sha: pid for pid, sha in load(ROOT / "fixtures" / "gold" / "page-sha.json").items()}
    gp = sha2gold.get(page["image_sha256"])
    return load(ROOT / "fixtures" / "gold" / f"{gp}.json")["lines"] if gp else None


def analyze(rec: dict, record: dict, regions: list[dict], view: dict, caps: dict) -> dict:
    meta = rec["request_meta"]
    parsed = page_read.validate_page(SimpleNamespace(error=rec["error"], parsed=rec["parsed"]),
                                     {"region_ids": meta["region_ids"], "table_ids": meta.get("table_ids") or []})
    ok = [rid for rid, v in parsed.items() if v["contract_ok"]]
    over = [rid for rid, v in parsed.items() if v["contract_ok"] and v.get("text") and len(v["text"]) > caps[rid]]
    reasons = {}
    for r in regions:
        why = page_read.verify_reasons(record, r, view, parsed[r["region_id"]])
        if r["region_id"] in over:
            why = why + ["page_field_truncated"]
        if why:
            reasons[r["region_id"]] = why
    gold = gold_for(record["page"])
    g = None
    if gold is not None:
        g = {"exact": 0, "approx": 0, "missing": 0, "unsupported": 0}
        for r in regions:
            e = text_eval.evaluate_region(parsed[r["region_id"]].get("text"), r["bbox"], gold)
            g["exact"] += e["lines_exact"]; g["approx"] += e["lines_approx_only"]
            g["missing"] += len(e["missing_lines"]); g["unsupported"] += e["unsupported_chars"]
    body = text_eval.norm("".join(parsed[r["region_id"]].get("text") or "" for r in regions))
    reps = {p: body.count(p) for p in LEGIT_REPEATS.get(meta["view_id"], [])}
    return {"finish": rec["finish_reason"], "error": rec["error"], "seconds": rec["seconds"],
            "tokens": (rec.get("usage") or {}).get("completion_tokens"), "page_ok": len(ok) == len(regions),
            "regions": len(regions), "regions_ok": len(ok), "over_cap": over, "reread_regions": len(reasons),
            "reread_reasons": sorted({x for v in reasons.values() for x in v}), "gold": g, "legit_repeats": reps,
            "texts": {rid: parsed[rid].get("text") for rid in parsed}}


def summarize(out: Path) -> None:
    orig = original_calls()
    ctx = {}
    for vid in FAIL_VIEWS + CONTROL_VIEWS:
        record, view, regions, _ = rebuild(vid)
        ctx[vid] = (record, regions, view, region_caps(record, regions))
    res = {"original_r1": {}}
    for vid, c in orig.items():
        if vid in ctx:
            res["original_r1"][vid] = [analyze(c, *ctx[vid])]
    for cond_dir in sorted((out / "calls").glob("*")):
        rows = {}
        for p in sorted(cond_dir.glob("*.json")):
            rec = load(p)
            vid = rec["request_meta"]["view_id"]
            rows.setdefault(vid, []).append(analyze(rec, *ctx[vid]))
        res[cond_dir.name] = rows
    lines = ["# docs/13 실험 결과", ""]
    for cond, rows in res.items():
        lines += [f"## {cond}", "", "| 판독 단위 | 회차 | 종료 | 출력토큰 | 초 | 영역 계약 통과 | 상한 초과 | 재판독 대상 | 정답(정확/근사/누락/근거없는 글자) | 실제 반복 |",
                  "|---|---|---|---:|---:|---|---|---:|---|---|"]
        for vid in FAIL_VIEWS + CONTROL_VIEWS:
            for k, a in enumerate(rows.get(vid, [])):
                g = a["gold"]
                gold = "-" if g is None else f"{g['exact']}/{g['approx']}/{g['missing']}/{g['unsupported']}"
                lines.append(f"| {vid} | {k} | {a['finish']} | {a['tokens']} | {a['seconds']:.0f} | {a['regions_ok']}/{a['regions']} | "
                             f"{','.join(x.split('-')[-1] for x in a['over_cap']) or '-'} | {a['reread_regions']} | "
                             f"{gold} | {a['legit_repeats'] or '-'} |")
        secs = [a["seconds"] for v in FAIL_VIEWS for a in rows.get(v, [])]
        fails = sum(not a["page_ok"] for v in FAIL_VIEWS for a in rows.get(v, []))
        if secs:
            lines += ["", f"실패 사례: 페이지 응답 실패 {fails}/{len(secs)}, 호출 시간 중앙값 {statistics.median(secs):.0f}초"]
        lines.append("")
    dump(out / "summary.json", res)
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["capability", "run", "summarize"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--condition", choices=["base", "I1", "I2"])
    ap.add_argument("--views", choices=["fail", "control", "all"], default="fail")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=12000)
    ap.add_argument("--concurrency", type=int, default=2)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "capability":
        capability(a.out)
    elif a.cmd == "run":
        if (a.out / "calls" / a.condition).exists() and any((a.out / "calls" / a.condition).glob(f"*")):
            existing = {p.stem.rsplit("-r", 1)[0] for p in (a.out / "calls" / a.condition).glob("*.json")}
            views = {"fail": FAIL_VIEWS, "control": CONTROL_VIEWS, "all": FAIL_VIEWS + CONTROL_VIEWS}[a.views]
            if existing & set(views):
                raise SystemExit(f"{a.condition}: 이미 기록된 판독 단위가 있다(덮어쓰기 방지) {sorted(existing & set(views))}")
        views = {"fail": FAIL_VIEWS, "control": CONTROL_VIEWS, "all": FAIL_VIEWS + CONTROL_VIEWS}[a.views]
        run(a.out, a.condition, views, a.reps, a.max_tokens, a.concurrency)
    else:
        summarize(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
