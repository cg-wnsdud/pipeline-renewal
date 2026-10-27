"""OCR 배정 → VLM 참고 후보 → 판독 단계 연결 감사(모델 호출 없음). docs/11 §1.

같은 Stage 1 원시 응답에 배정 규칙(core_shrink)만 바꿔 다시 계산하고, 항목마다 다음을 연결한다.
  - 원래 OCR bbox, 영역별 포함 비율·돌출량·core 판정, 규칙별 배정/미배정 사유
  - 규칙별로 각 영역 요청에 들어가는 참고 후보(crop/3·page/2·reread 표현, crop/1 표현)
  - 실제 저장 요청(trace/vlm)의 후보와 재계산 후보가 같은지(재현 검사)
완화 시 바뀌는 항목은 확대 시트(scripts/audit_assignment.tile)로 남기고, 판정은 별도 시각 검토 파일에 기록한다.
배정 규칙·bbox 는 바꾸지 않는다.

사용: audit_reference_flow.py --stage1 <dir> --run <page-first run> --run <crop run> --out <dir> [--cases <json>]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_assignment import COLS, LABEL_H, ROWS, TILE_H, TILE_W, ink_outside, tile  # noqa: E402
from layout_pipeline import vlm_tasks  # noqa: E402
from layout_pipeline.assignment import AssignmentRule, assign  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.regions import parse_view  # noqa: E402
from layout_pipeline.routing import InputView  # noqa: E402
from layout_pipeline.stage1 import resolve  # noqa: E402
from layout_pipeline.text_eval import norm  # noqa: E402

RULES = {"strict": 0.0, "core0.1": 0.1, "core0.25": 0.25}


def load_pages(stage1: Path) -> dict[str, dict]:
    """최종 view(whole·tile·candidate_tile)의 원시 응답만 쓴다. 탐색(scout) 응답은 영역·OCR 분모에 넣지 않는다."""
    calls = {c["view_id"]: c for c in load(stage1 / "calls.json") if c["ok"]}
    out = {}
    for route in load(stage1 / "routes.json"):
        if not route["views"]:
            continue
        regions, items = [], []
        for v in route["views"]:
            view = InputView(v["view_id"], v["page_id"], v["kind"], tuple(v["crop"]))
            raw = load(resolve(calls[v["view_id"]]["raw_path"]))["result"]["layoutParsingResults"][0]["prunedResult"]
            r, i, _ = parse_view(raw, view)
            regions += r
            items += i
        out[route["page_id"]] = {"regions": regions, "items": items, "record": load(stage1 / "records" / f"{route['page_id']}.json")}
    return out


def refs(record_like: dict, region: dict) -> dict:
    """규칙별 요청 후보 두 표현. ID 집합은 비교용이며 요청 본문에는 ID 가 없다(crop/3)."""
    ctx = vlm_tasks.region_context(record_like, region)
    v3 = [c for c in ctx["ocr_candidates"] if c["source"] == "assigned"
          or c["inside_ratio"] >= vlm_tasks.BOUNDARY_CANDIDATE_MIN_INSIDE]
    return {"crop3_ids": sorted(c["ocr_id"] for c in v3),
            "crop3_labeled": sorted((c["ocr_id"], "in_region" if c["source"] == "assigned" else "boundary") for c in v3),
            "crop1_ids": sorted(c["ocr_id"] for c in ctx["ocr_candidates"]),
            "crop1_labeled": sorted((c["ocr_id"], "draft" if c["source"] == "assigned" else "boundary") for c in ctx["ocr_candidates"]),
            "crop3_payload": vlm_tasks.candidate_context(record_like, region, "crop/3")["ocr_candidates"],
            "crop1_payload": vlm_tasks.legacy_context(record_like, region)}


def stored_requests(run: Path) -> dict[str, list[dict]]:
    """region_id → 저장 요청에 실제로 들어간 후보(호출 종류별)."""
    out: dict[str, list[dict]] = {}
    for p in sorted((run / "trace" / "vlm").glob("*.json")):
        rec = load(p)
        meta = rec["request_meta"]
        for it in meta.get("items") or []:
            payload = {k: it[k] for k in ("ocr_candidates", "ocr_draft", "boundary_ocr") if k in it}
            out.setdefault(it["region_id"], []).append({"call_id": rec["call_id"], "kind": meta["kind"],
                                                        "version": meta.get("version"), "payload": payload})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1", type=Path, required=True)
    ap.add_argument("--run", type=Path, action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cases", type=Path, default=None, help="오류 사례 핵심 문구(평가 전용)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    pages = load_pages(a.stage1)
    font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 12)
    items_out, region_changes, changed, repro = [], [], [], []
    stored = {run.name: stored_requests(run) for run in a.run}
    for pid, pg in pages.items():
        regions, items, record = pg["regions"], pg["items"], pg["record"]
        res = {k: assign(items, regions, AssignmentRule(core_shrink=v)) for k, v in RULES.items()}
        # Stage 1 기록과 strict 재계산이 같은지(저장 응답 재현)
        rec_assigned = {x["ocr_id"]: x["region_id"] for x in record["assignments"]}
        re_assigned = {x["ocr_id"]: x["region_id"] for x in res["strict"]["assigned"]}
        repro.append({"page_id": pid, "stage1_record_matches_strict_recompute": rec_assigned == re_assigned,
                      "checks": {k: {kk: v["checks"][kk] for kk in ("ocr_items", "assigned", "unassigned", "passed")}
                                 for k, v in res.items()}})
        status = {k: ({x["ocr_id"]: ("assigned", x["region_id"]) for x in v["assigned"]}
                      | {x["ocr_id"]: (x["reason"], None) for x in v["unassigned_texts"]}) for k, v in res.items()}
        cands = {x["ocr_id"]: x["candidates"] for x in res["strict"]["unassigned_texts"]}
        likes = {k: {"assignments": v["assigned"], "unassigned_texts": v["unassigned_texts"]} for k, v in res.items()}
        reg_dicts = [r.to_dict() for r in regions]
        rref = {k: {r["region_id"]: refs(likes[k], r) for r in reg_dicts} for k in RULES}
        for r in reg_dicts:
            rid = r["region_id"]
            row = {"page_id": pid, "region_id": rid}
            for k in ("core0.1", "core0.25"):
                s, c = rref["strict"][rid], rref[k][rid]
                row[k] = {"crop3_id_set_changed": s["crop3_ids"] != c["crop3_ids"],
                          "crop3_label_changed": s["crop3_labeled"] != c["crop3_labeled"],
                          "crop3_payload_changed": s["crop3_payload"] != c["crop3_payload"],
                          "crop1_id_set_changed": s["crop1_ids"] != c["crop1_ids"],
                          "crop1_payload_changed": s["crop1_payload"] != c["crop1_payload"],
                          "crop1_ids_removed": sorted(set(s["crop1_ids"]) - set(c["crop1_ids"])),
                          "crop1_ids_added": sorted(set(c["crop1_ids"]) - set(s["crop1_ids"]))}
            # 저장 요청 재현: strict 재계산 후보 == 실제 요청 후보
            for run_name, by_region in stored.items():
                for st in by_region.get(rid, []):
                    p = st["payload"]
                    if "ocr_candidates" in p:
                        ok = p["ocr_candidates"] == rref["strict"][rid]["crop3_payload"]
                    else:
                        exp = rref["strict"][rid]["crop1_payload"]
                        ok = p.get("ocr_draft") == exp["ocr_draft"] and p.get("boundary_ocr") == exp["boundary_ocr"]
                    row.setdefault("stored_request_repro", []).append({"run": run_name, "call_id": st["call_id"],
                                                                       "kind": st["kind"], "matches_strict": ok})
            region_changes.append(row)
        img = None
        by_region = {r.region_id: r for r in regions}
        for it in items:
            st = {k: status[k][it.ocr_id] for k in RULES}
            in_refs = {k: sorted(rid for rid, v in rref[k].items() if it.ocr_id in v["crop3_ids"]) for k in RULES}
            in_refs1 = {k: sorted(rid for rid, v in rref[k].items() if it.ocr_id in v["crop1_ids"]) for k in RULES}
            entry = {"ocr_id": it.ocr_id, "page_id": pid, "view_id": it.view_id, "text": it.ocr_text, "empty": it.empty,
                     "bbox": [round(v, 1) for v in it.ocr_bbox], "score": round(it.score, 3),
                     "candidates": cands.get(it.ocr_id) or [{"region_id": status["strict"][it.ocr_id][1], "exact_inside": True}],
                     "status": {k: {"status": v[0], "region_id": v[1]} for k, v in st.items()},
                     "crop3_reference_regions": in_refs, "crop1_reference_regions": in_refs1}
            items_out.append(entry)
            for k in ("core0.1", "core0.25"):
                if st["strict"][0] != "assigned" and st[k][0] == "assigned":
                    if img is None:
                        img = Image.open(record["page"]["image_path"]).convert("RGB")
                        arr = np.asarray(img)
                    rb = by_region[st[k][1]].bbox
                    ink = ink_outside(arr, it.ocr_bbox, rb)
                    c = next(x for x in cands[it.ocr_id] if x["region_id"] == st[k][1])
                    others = [x for x in cands[it.ocr_id] if x["region_id"] != st[k][1]]
                    changed.append({"rule": k, "ocr_id": it.ocr_id, "page_id": pid, "region_id": st[k][1], "text": it.ocr_text,
                                    "strict_reason": st["strict"][0], "ocr_bbox": it.ocr_bbox, "region_bbox": rb,
                                    "inside_ratio": c["inside_ratio"], "protrusion_px": c["protrusion_px"],
                                    "short_side_px": round(min(it.ocr_bbox[2] - it.ocr_bbox[0], it.ocr_bbox[3] - it.ocr_bbox[1]), 1),
                                    "protrusion_sides": {s: round(v, 2) for s, v in (("left", rb[0] - it.ocr_bbox[0]),
                                                                                     ("top", rb[1] - it.ocr_bbox[1]),
                                                                                     ("right", it.ocr_bbox[2] - rb[2]),
                                                                                     ("bottom", it.ocr_bbox[3] - rb[3])) if v > 0},
                                    "other_regions": others, "_img": img, **ink})
    # 0.25 에서 바뀌는 항목 전체(0.1 은 그 부분집합인지 확인)
    c25 = [x for x in changed if x["rule"] == "core0.25"]
    c10 = {x["ocr_id"] for x in changed if x["rule"] == "core0.1"}
    c25.sort(key=lambda x: (x["page_id"], -x["protrusion_px"]))
    sheets = []
    for n in range(0, len(c25), COLS * ROWS):
        chunk = c25[n:n + COLS * ROWS]
        sheet = Image.new("RGB", (COLS * TILE_W, ((len(chunk) + COLS - 1) // COLS) * (TILE_H + LABEL_H)), "white")
        for k, x in enumerate(chunk):
            x["sheet_index"] = n + k + 1
            label = (f"#{x['sheet_index']} {x['ocr_id'].replace('_p01', '')} 돌출{x['protrusion_px']}px "
                     f"{','.join(x['protrusion_sides'])}{' (0.1도)' if x['ocr_id'] in c10 else ''}\n{x['text'][:34]}")
            t = tile(x["_img"], x["ocr_bbox"], x["region_bbox"], x["focus"], label, font)
            sheet.paste(t, ((k % COLS) * TILE_W, (k // COLS) * (TILE_H + LABEL_H)))
        path = a.out / f"changed-sheet-{len(sheets) + 1}.png"
        sheet.save(path)
        sheets.append(path.as_posix())
    review_path = a.out / "changed-review.json"
    verdicts = load(review_path)["verdicts"] if review_path.is_file() else {}
    for x in changed:
        x.pop("_img", None)
        x["in_core0.1"] = x["ocr_id"] in c10
        v = verdicts.get(x["ocr_id"]) or {}
        x["verdict"], x["verdict_note"] = v.get("verdict", "not_reviewed"), v.get("note")
    summary = {
        "repro": repro,
        "rules": {k: {"assigned": sum(p["checks"][k]["assigned"] for p in repro),
                      "unassigned": sum(p["checks"][k]["unassigned"] for p in repro)} for k in RULES},
        "changed": {k: sum(x["rule"] == k for x in changed) for k in ("core0.1", "core0.25")},
        "core0.1_subset_of_core0.25": c10 <= {x["ocr_id"] for x in c25},
        "region_reference_changes": {k: {m: sum(r[k][m] for r in region_changes)
                                         for m in ("crop3_id_set_changed", "crop3_label_changed", "crop3_payload_changed",
                                                   "crop1_id_set_changed", "crop1_payload_changed")}
                                     for k in ("core0.1", "core0.25")},
        "stored_request_repro": dict(Counter((x["run"], x["kind"], x["matches_strict"]) for r in region_changes
                                             for x in r.get("stored_request_repro", [])).items()),
        "verdict_counts": dict(Counter(x["verdict"] for x in c25)),
        "sheets": sheets,
    }
    summary["stored_request_repro"] = [{"run": k[0], "kind": k[1], "matches_strict": k[2], "n": v}
                                       for k, v in summary["stored_request_repro"].items()]
    if a.cases:
        summary["cases"] = case_table(load(a.cases), items_out, region_changes, a.run)
    dump(a.out / "items.json", items_out)
    dump(a.out / "region-reference-changes.json", region_changes)
    dump(a.out / "changed.json", sorted(changed, key=lambda x: (x["rule"], x.get("sheet_index") or 0)))
    dump(a.out / "summary.json", summary)
    print({k: v for k, v in summary.items() if k not in ("cases",)})
    return 0


def case_table(cases: list[dict], items: list[dict], region_changes: list[dict], runs: list[Path]) -> list[dict]:
    """오류 사례: 핵심 문구가 원시 OCR 에 있었는지, 어느 영역 참고로 갔는지, 판독 단계별 문구에 있었는지."""
    reads = {run.name: load(run / "trace" / "reads.json") for run in runs}
    finals = {run.name: {g["region_id"]: g for g in load(run / "result.json")["regions"]} for run in runs}
    out = []
    for c in cases:
        rid = c["region_id"]
        row = {"case": c["case"], "region_id": rid, "phrases": []}
        for ph in c["phrases"]:
            n = norm(ph)
            hits = [it for it in items if it["page_id"] == rid.split("-")[0] and n and
                    (n in norm(it["text"]) or (len(norm(it["text"])) >= 4 and norm(it["text"]) in n))]
            stage = {}
            for name, rd in reads.items():
                tr = (rd.get(rid) or {}).get("trace") or {}
                for label, t in (("page", tr.get("page_text")), ("first", tr.get("first_text")),
                                 ("reread", tr.get("reread_text")), ("final", finals[name].get(rid, {}).get("text"))):
                    if t is not None:
                        # 정규화로 사라지는 기호('|' 등)는 원문 그대로 찾는다
                        stage[f"{name}:{label}"] = (n in norm(t)) if n else (ph in t)
            row["phrases"].append({"phrase": ph, "raw_ocr": [{"ocr_id": h["ocr_id"], "text": h["text"],
                                                              "strict": h["status"]["strict"],
                                                              "crop3_ref_regions": h["crop3_reference_regions"]["strict"]}
                                                             for h in hits], "in_stage_text": stage})
        out.append(row)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
