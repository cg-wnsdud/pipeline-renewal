"""긴 이미지 입력 크기 비교(docs/05-long-input-plan.md 고정 계획). 경계 확정·최종 타일 OCR 이 아니다.

  run  : 큰 창·페이지 전체를 원본 해상도로 OCR(W4 = 폭×4 창, WMAX = 4000px 창, WHOLE = 페이지 전체)
  eval : 기존 탐색 창(S25 = 폭×2.5, 50% 겹침, work/boundary/exp1)과 함께 정답 대조

창 가장자리에 닿은 상자(페이지 끝 제외)는 잘린 관측으로 빼고, 여러 창에서 관측한 상자를 합쳐 페이지 단위로 센다.
사용: run_long_input_experiment.py run --out work/long/exp2 / eval --out work/long/exp2
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import boundary  # noqa: E402
from layout_pipeline.config import get_settings, load_env_file  # noqa: E402
from layout_pipeline.geometry import area, contains, intersection  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.ocr_client import BASE_OPTIONS, OcrClient, text_det_resize_estimate  # noqa: E402
from layout_pipeline.pages import Page  # noqa: E402
from layout_pipeline.routing import InputView  # noqa: E402
from layout_pipeline.structure_eval import content  # noqa: E402
from layout_pipeline.text_eval import membership, norm  # noqa: E402

PAGES_JSON = ROOT / "work" / "runs" / "stage1-merge-large" / "pages.json"
SCOUT_DIR = ROOT / "work" / "boundary" / "exp1"
PAGE_IDS = ["f08_p01", "f09_p01", "f11_p01"]
TEXT_DET_MAX_SIDE = 4000     # 서버 보고 text_det_params.max_side_limit


def windows(width: int, height: int, length: int) -> list[tuple[int, int, int, int]]:
    """길이 length 창을 처음과 끝에 맞추고 사이를 균등 간격으로 채운다(겹침 ≥ 1/3 창)."""
    if height <= length:
        return [(0, 0, width, height)]
    n = 2
    while (height - length) / (n - 1) > length * 2 / 3:
        n += 1
    step = (height - length) / (n - 1)
    return [(0, round(i * step), width, round(i * step) + length) for i in range(n)]


def configs(page: Page) -> dict[str, list]:
    out = {"WHOLE": [(0, 0, page.width, page.height)],
           "WMAX": windows(page.width, page.height, TEXT_DET_MAX_SIDE),
           "W4": windows(page.width, page.height, 4 * page.width)}
    # 같은 창 목록이면 한 번만 호출(예: f11 은 W4·WMAX·WHOLE 이 모두 페이지 전체)
    seen, uniq = {}, {}
    for k, w in out.items():
        key = tuple(w)
        uniq[k] = seen.setdefault(key, k)
    return {k: out[k] for k in out if uniq[k] == k} | {"_alias": {k: v for k, v in uniq.items() if v != k}}


def run(out: Path) -> None:
    settings = get_settings()
    import json
    layout = json.loads((ROOT / "config" / "layout-options.json").read_text(encoding="utf-8"))["layout_options"]
    client = OcrClient(settings.ocr_url, settings.ocr_timeout_s, dict(BASE_OPTIONS) | layout, out / "raw", out / "views")
    pages = {p["page_id"]: Page(**{k: v for k, v in p.items() if k != "geometry"}) for p in load(PAGES_JSON)}
    plan = {}
    for pid in PAGE_IDS:
        page = pages[pid]
        cfg = configs(page)
        plan[pid] = {"alias": cfg.pop("_alias"), "configs": {}}
        for name, wins in cfg.items():
            calls = []
            for i, crop in enumerate(wins):
                view = InputView(f"{pid}-{name}-{i}", pid, "whole" if crop == (0, 0, page.width, page.height) else "tile", crop)
                rec, _ = client.call(page, view)
                calls.append(asdict(rec))
                print(pid, name, crop, rec.ok, rec.counts, rec.seconds, flush=True)
            plan[pid]["configs"][name] = calls
    dump(out / "calls.json", {"plan": "docs/05-long-input-plan.md", "layout_options": layout, "pages": plan})


def load_config_views(out: Path) -> dict[str, dict[str, list]]:
    """page → config → [(InputView, pruned)]. S25 는 기존 탐색 결과를 그대로 읽는다(새 호출 없음)."""
    res: dict[str, dict[str, list]] = {}
    scout = load(SCOUT_DIR / "scout.json")["pages"]
    for pid in PAGE_IDS:
        views = []
        for i, (crop, call) in enumerate(zip(scout[pid]["windows"], scout[pid]["calls"])):
            data = load(SCOUT_DIR / "raw" / f"{call['call_id']}.json")
            views.append((InputView(f"{pid}-s{i}", pid, "scout", tuple(crop)), data["result"]["layoutParsingResults"][0]["prunedResult"]))
        res[pid] = {"S25": views}
    calls = load(out / "calls.json")["pages"]
    for pid in PAGE_IDS:
        for name, recs in calls[pid]["configs"].items():
            res[pid][name] = [(InputView(r["view_id"], pid, r["view_kind"], tuple(r["input_crop"])),
                               load(Path(r["raw_path"]))["result"]["layoutParsingResults"][0]["prunedResult"]) for r in recs]
        for alias, src in calls[pid]["alias"].items():
            res[pid][alias] = res[pid][src]
    return res


def best_ocr(gb, ocr: list[dict]):
    best, best_iou = None, 0.0
    for o in ocr:
        inter = intersection(gb, o["bbox"])
        if inter <= 0:
            continue
        iou = inter / (area(gb) + area(o["bbox"]) - inter)
        if iou > best_iou:
            best, best_iou = o, iou
    return best if best_iou >= 0.5 else None


def final_boxes(pruned: dict, view: InputView) -> list[dict]:
    """최종 타일(겹침 없음)의 상자 전부(페이지 좌표). 탐색 창과 달리 가장자리 상자를 빼지 않는다."""
    from layout_pipeline.regions import parse_view
    regions, items, _ = parse_view(pruned, view)
    return ([{"kind": "layout", "bbox": r.bbox, "label": r.raw_label, "text": None} for r in regions] +
            [{"kind": "ocr", "bbox": o.ocr_bbox, "text": o.ocr_text, "label": None} for o in items if o.ocr_text.strip()])


def evaluate(page_h: int, views: list, gold: dict, tables: list[dict], final: bool = False) -> dict:
    obs = [b for v, p in views for b in (final_boxes(p, v) if final else boundary.observed_boxes(p, v, page_h))]
    layout = [b for b in obs if b["kind"] == "layout"]
    ocr = [b for b in obs if b["kind"] == "ocr"]
    lines = [g for g in gold["lines"] if not g.get("optional")]
    home = exact = cexact = assignable = no_ocr = 0
    misses = []
    for g in lines:
        gb = [float(v) for v in g["bbox"]]
        h = any(membership(gb, b["bbox"]) == "in" for b in layout)
        o = best_ocr(gb, ocr)
        home += h
        if o is None:
            no_ocr += 1
        else:
            exact += norm(o["text"]) == norm(g["text"])
            cexact += content(o["text"]) == content(g["text"])
            assignable += any(contains(b["bbox"], o["bbox"]) for b in layout)
        if not h or o is None or content(o["text"]) != content(g["text"]):
            misses.append({"gold": g["id"], "text": g["text"], "home": h, "ocr": None if o is None else o["text"]})
    by_id = {g["id"]: g for g in gold["lines"]}
    tab = []
    for t in tables:
        ids = [f"{t['page']}-{x}" for row in t["rows"] for cell in row for x in cell]
        boxes = [[float(v) for v in by_id[i]["bbox"]] for i in ids if i in by_id]
        whole = any(b["label"] == "table" and all(membership(gb, b["bbox"]) == "in" for gb in boxes) for b in layout)
        tab.append({"id": t["id"], "lines": len(boxes), "single_table_box": whole})
    resize = []
    for v, p in views:
        est = text_det_resize_estimate(v.size[0], v.size[1], (p.get("overall_ocr_res") or {}).get("text_det_params"))
        resize.append(None if est is None else est["ratio"])
    n = len(lines)
    return {"views": len(views), "lines": n, "layout_home": home, "ocr_exact": exact, "ocr_content_exact": cexact,
            "ocr_assignable": assignable, "no_ocr": no_ocr, "rates": {"layout_home": round(home / n, 3),
            "ocr_content_exact": round(cexact / n, 3), "ocr_assignable": round(assignable / n, 3)},
            "layout_boxes_observed": len(layout), "table_boxes": sum(b["label"] == "table" for b in layout),
            "gold_tables": tab, "text_det_ratio": resize, "misses": misses}


def candidate_views(stage1: Path) -> dict[str, list]:
    calls = load(stage1 / "calls.json")
    res: dict[str, list] = {}
    for c in calls:
        if c["page_id"] in PAGE_IDS and c["ok"] and c["view_kind"] == "candidate_tile":
            raw = load(Path(c["raw_path"]) if Path(c["raw_path"]).is_absolute() else ROOT / c["raw_path"])
            res.setdefault(c["page_id"], []).append((InputView(c["view_id"], c["page_id"], "candidate_tile", tuple(c["input_crop"])),
                                                     raw["result"]["layoutParsingResults"][0]["prunedResult"]))
    return res


def report(out: Path, cand_stage1: Path | None = None) -> None:
    res = load_config_views(out)
    cand = candidate_views(cand_stage1) if cand_stage1 else {}
    pages = {p["page_id"]: p for p in load(PAGES_JSON)}
    rel = load(ROOT / "fixtures" / "gold-relations.json")["tables"]
    rows, data = [], {}
    for pid in PAGE_IDS:
        gold = load(ROOT / "fixtures" / "gold" / f"{pid}.json")
        data[pid] = {}
        names = ["S25", "W4", "WMAX", "WHOLE"] + (["CAND"] if pid in cand else [])
        for name in names:
            views = cand[pid] if name == "CAND" else res[pid][name]
            e = evaluate(pages[pid]["height"], views, gold, [t for t in rel if t["page"] == pid], final=name == "CAND")
            if name == "CAND":
                # 픽셀 범위 덮기와 의미 단위 보존을 따로 본다: 타일이 원본을 빈틈·중복 없이 덮는지, 정답 줄·표가 경계를 가로지르는지.
                spans = sorted((v.crop[1], v.crop[3]) for v, _ in views)
                cuts = [y for _, y in spans[:-1]]
                e["tiles"] = [list(x) for x in spans]
                e["covers_without_gap_or_overlap"] = spans[0][0] == 0 and spans[-1][1] == pages[pid]["height"] and                     all(a2 == b1 for (_, b1), (a2, _) in zip(spans, spans[1:]))
                e["gold_lines_crossing_cuts"] = [g["id"] for g in gold["lines"] if any(g["bbox"][1] < y < g["bbox"][3] for y in cuts)]
                by = {g["id"]: g for g in gold["lines"]}
                e["gold_tables_crossing_cuts"] = [t["id"] for t in rel if t["page"] == pid and any(
                    min(by[f"{pid}-{x}"]["bbox"][1] for row in t["rows"] for c in row for x in c) < y <
                    max(by[f"{pid}-{x}"]["bbox"][3] for row in t["rows"] for c in row for x in c) for y in cuts)]
            data[pid][name] = e
            rows.append(f"| {pid} | {name} | {e['views']} | {e['lines']} | {e['layout_home']} | {e['ocr_assignable']} | "
                        f"{e['ocr_content_exact']} / {e['ocr_exact']} | {e['no_ocr']} | {e['layout_boxes_observed']} | "
                        f"{e['table_boxes']} | {', '.join(f'{t['id']}:{'예' if t['single_table_box'] else '아니오'}' for t in e['gold_tables']) or '-'} | "
                        f"{e['text_det_ratio']} |")
    md = ["# 긴 이미지 입력 크기 비교", "", "계획: docs/05-long-input-plan.md. S25 = 기존 탐색(폭×2.5 창 50% 겹침, 재사용), W4 = 폭×4 창, "
          "WMAX = 4000px 창, WHOLE = 페이지 전체, CAND = 후보 경계 최종 타일(겹침 없음, 가장자리 상자 포함, docs/08). "
          "S25·W4·WMAX 는 창 가장자리에 잘린 상자를 빼고 여러 창 관측을 합쳐 센다(겹침 창 중 좋은 관측을 고르는 셈이라 CAND 보다 유리).", "",
          "| 페이지 | 입력 | 호출 | 정답 줄 | 레이아웃 상자 안(50%) | OCR 줄이 상자에 엄격 포함 | OCR 정답(글자·숫자/기호 포함) | 같은 위치 OCR 없음 | 관측 레이아웃 상자 | 표 상자 | 정답 표가 한 표 상자 안 | 텍스트 검출 배율 |",
          "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|---|"] + rows
    dump(out / "eval.json", data)
    (out / "eval.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "eval"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--candidate-stage1", type=Path, default=None, help="후보 타일 OCR 을 실행한 Stage 1 폴더(CAND 조건)")
    a = ap.parse_args()
    load_env_file()
    if a.cmd == "run":
        if (a.out / "calls.json").exists():
            ap.error("이미 실행된 폴더입니다(측정 덮어쓰기 방지)")
        run(a.out)
    report(a.out, a.candidate_stage1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
