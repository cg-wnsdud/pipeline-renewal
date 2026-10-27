"""긴 이미지 자동 경계 실험(docs/boundary-experiment-plan.md 고정 계획).

  scout    : 탐색 OCR(비율 2.5 창, 50% 겹침) → 보호 구간 → 기하 후보 + 라벨링용 시트
  classify : 고정 라벨 사례에 M1(간격 규칙)·M2(VLM 관계 분류)·M1∧M2 적용 → 기준 평가 → 경계열 구성 → candidate 계획

사용:
  run_boundary_experiment.py scout --out work/boundary/exp1
  run_boundary_experiment.py classify --out work/boundary/exp1 --labels fixtures/boundary-labels.json
결과의 어떤 계획도 validated 가 아니며 최종 타일 OCR 을 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import boundary  # noqa: E402
from layout_pipeline.config import get_settings, load_env_file  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.ocr_client import BASE_OPTIONS, OcrClient  # noqa: E402
from layout_pipeline.pages import Page  # noqa: E402
from layout_pipeline.routing import InputView, compose_sequence  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402

PAGES_JSON = ROOT / "work" / "runs" / "stage1-merge-large" / "pages.json"
PAGE_IDS = ["f08_p01", "f09_p01", "f11_p01"]


def load_pages() -> dict[str, Page]:
    return {p["page_id"]: Page(**{k: v for k, v in p.items() if k != "geometry"}) for p in load(PAGES_JSON)}


def scout(out: Path) -> None:
    settings = get_settings()
    layout = json.loads((ROOT / "config" / "layout-options.json").read_text(encoding="utf-8"))["layout_options"]
    client = OcrClient(settings.ocr_url, settings.ocr_timeout_s, dict(BASE_OPTIONS) | layout, out / "raw", out / "views")
    pages = load_pages()
    font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 18)
    result = {}
    for pid in PAGE_IDS:
        page = pages[pid]
        boxes, calls = [], []
        for i, crop in enumerate(boundary.scout_windows(page.width, page.height)):
            view = InputView(f"{pid}-s{i}", pid, "scout", crop)
            rec, pruned = client.call(page, view)
            calls.append(asdict(rec))
            if pruned is not None:
                boxes += boundary.observed_boxes(pruned, view, page.height)
        cands, line_gap = boundary.candidates(boxes, page.height)
        for k, c in enumerate(cands):
            c["id"] = f"{pid}-c{k:02d}"
            c["m1_safe"] = c["gap_px"] >= boundary.M1_GAP_RATIO * line_gap
        result[pid] = {"size": [page.width, page.height], "windows": [c["input_crop"] for c in calls],
                       "calls": [{k: c[k] for k in ("call_id", "seconds", "ok", "error", "counts")} for c in calls],
                       "line_gap_median": line_gap, "boxes": boxes, "candidates": cands}
        # 라벨링용 시트: 후보선 위아래 150px, 원본 + 빨간 선(별도 사본)
        img = page.open_image()
        tiles = []
        for c in cands:
            top, bot = max(0, c["y"] - 150), min(page.height, c["y"] + 150)
            t = img.crop((0, top, page.width, bot)).copy()
            ImageDraw.Draw(t).line((0, c["y"] - top, page.width, c["y"] - top), fill=(220, 0, 0), width=2)
            canvas = Image.new("RGB", (page.width, t.height + 26), "white")
            canvas.paste(t, (0, 26))
            ImageDraw.Draw(canvas).text((4, 2), f"{c['id']} y={c['y']} gap={c['gap_px']} M1={'safe' if c['m1_safe'] else '-'}",
                                        font=font, fill=(0, 0, 0))
            tiles.append(canvas)
        for s in range(0, len(tiles), 12):
            chunk = tiles[s:s + 12]
            cols = 3
            rows = (len(chunk) + cols - 1) // cols
            h = max(t.height for t in chunk)
            sheet = Image.new("RGB", (cols * (page.width + 8), rows * (h + 8)), (220, 220, 220))
            for k, t in enumerate(chunk):
                sheet.paste(t, ((k % cols) * (page.width + 8), (k // cols) * (h + 8)))
            sheet.save(out / f"label-{pid}-{s // 12 + 1}.png")
        print(pid, "windows", len(calls), "boxes", len(boxes), "candidates", len(cands), "line_gap", line_gap, flush=True)
    dump(out / "scout.json", {"plan": "docs/boundary-experiment-plan.md", "scout_aspect": boundary.SCOUT_ASPECT,
                              "note": "탐색 결과는 문맥·보호 위치 확인용. 최종 Region 아님.", "pages": result})


def classify(out: Path, labels_path: Path) -> None:
    load_env_file()
    scout_data = load(out / "scout.json")["pages"]
    labels = load(labels_path)["cases"]
    pages = load_pages()
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                       prefix="bnd")
    rows = []
    images = {}
    for case in labels:
        pid = case["page_id"]
        sd = scout_data[pid]
        cand = min(sd["candidates"], key=lambda c: abs(c["y"] - case["y"]))
        if abs(cand["y"] - case["y"]) > 3:
            raise SystemExit(f"{case['id']}: y={case['y']} 에 해당하는 기하 후보가 없습니다")
        images.setdefault(pid, pages[pid].open_image())
        above, below = boundary.context_items(sd["boxes"], cand["y"])
        req = boundary.relation_request(images[pid], cand["y"], above, below)
        call = client.chat(req["text"], req["images"], req["schema"], "boundary_relation", 800,
                           {"case": case["id"], "page_id": pid, "y": cand["y"]})
        rec = asdict(call)
        dump(out / "calls" / f"{call.call_id}.json", rec)
        m2, relation = boundary.accept_m2(call.parsed, above, below)
        rows.append({**case, "candidate_id": cand["id"], "y": cand["y"], "gap_px": cand["gap_px"], "m1_safe": cand["m1_safe"],
                     "m2_safe": m2, "m2_relation": relation, "m2_parsed": call.parsed, "call_id": call.call_id,
                     "finish_reason": call.finish_reason, "error": call.error,
                     "m12_safe": cand["m1_safe"] and m2})
        print(case["id"], case["label"], "M1", cand["m1_safe"], "M2", m2, relation, flush=True)
    methods = {}
    for m in ("m1_safe", "m2_safe", "m12_safe"):
        false_safe = [r["id"] for r in rows if r["label"] == "unsafe" and r[m]]
        safe_rows = [r for r in rows if r["label"] == "safe"]
        recall = sum(r[m] for r in safe_rows) / len(safe_rows) if safe_rows else None
        methods[m] = {"false_safe": false_safe, "safe_recall": recall,
                      "passed": not false_safe and recall is not None and recall >= 0.5,
                      "by_category": {}}
        for r in rows:
            cat = methods[m]["by_category"].setdefault(r["category"], {"n": 0, "accepted": 0})
            cat["n"] += 1
            cat["accepted"] += bool(r[m])
    sequences = {}
    for pid in PAGE_IDS:
        sd = scout_data[pid]
        w, h = sd["size"]
        lab_safe = [r["y"] for r in rows if r["page_id"] == pid and r["label"] == "safe"]
        m1_all = [c["y"] for c in sd["candidates"] if c["m1_safe"]]
        m2_lab = [r["y"] for r in rows if r["page_id"] == pid and r["m2_safe"]]
        sequences[pid] = {name: compose_sequence(h, pos, round(w * boundary.SCOUT_ASPECT), cross=w,
                                                 max_aspect=boundary.SCOUT_ASPECT)
                          for name, pos in (("labels_safe", lab_safe), ("m1_all_candidates", m1_all), ("m2_labeled", m2_lab))}
    report = {"plan": "docs/boundary-experiment-plan.md", "cases": rows, "methods": methods, "sequences": sequences,
              "vlm_calls": client.count,
              "status": "candidate only: 자동 의미 경계 승인 방식 없음(ACCEPTED_SEMANTIC_METHODS=human_review). 최종 타일 OCR 미실행."}
    dump(out / "classify.json", report)
    print(json.dumps({"methods": methods, "sequences": {p: {k: v["status"] for k, v in s.items()} for p, s in sequences.items()}},
                     ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scout", "classify"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--labels", type=Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "scout":
        if (a.out / "scout.json").exists():
            raise SystemExit("scout.json 이 이미 있습니다(측정 덮어쓰기 방지)")
        scout(a.out)
    else:
        classify(a.out, a.labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
