"""레이아웃 임계값만 바꾼 읽기 전용 진단 요청(서버 설정 변경 없음). 최종 결과에 쓰지 않는다.

실행과 같은 요청(BASE_OPTIONS + config/layout-options.json)에 layoutThreshold(숫자 하나)만 더해 보낸다.
원시 응답을 저장하고, 지정한 OCR 줄(문구 일부)을 덮는 레이아웃 상자가 생기는지 기록한다.

사용: probe_layout_threshold.py --page-image <png> --threshold 0.3 0.2 --text 상품안내장 --text DC·IRP --out <새 폴더>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.geometry import area, intersection  # noqa: E402
from layout_pipeline.jsonio import dump  # noqa: E402
from layout_pipeline.ocr_client import BASE_OPTIONS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page-image", type=Path, required=True)
    ap.add_argument("--threshold", type=float, nargs="+", required=True)
    ap.add_argument("--text", action="append", default=[], help="확인할 OCR 줄 문구(부분 일치)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists() and any(a.out.iterdir()):
        ap.error(f"출력 폴더가 비어 있지 않습니다: {a.out}")
    load_env_file()
    url, timeout = os.environ["PN_OCR_URL"], float(os.environ.get("PN_OCR_TIMEOUT_S", "300"))
    layout = json.loads((ROOT / "config" / "layout-options.json").read_text(encoding="utf-8"))["layout_options"]
    blob = base64.b64encode(a.page_image.read_bytes()).decode("ascii")
    summary = {"page_image": str(a.page_image), "base_options": dict(BASE_OPTIONS) | layout, "probes": [],
               "statement": "읽기 전용 진단 요청. 서버 설정은 바꾸지 않았고 결과는 최종 결과에 쓰지 않는다."}
    for th in a.threshold:
        opts = dict(BASE_OPTIONS) | layout | {"layoutThreshold": th}
        t = time.perf_counter()
        resp = requests.post(url, json={"fileType": 1, "file": blob, **opts}, timeout=(10, timeout))
        sec = round(time.perf_counter() - t, 2)
        data = resp.json()
        dump(a.out / f"raw-threshold-{th}.json", data)
        pr = data["result"]["layoutParsingResults"][0]["prunedResult"]
        boxes = pr["layout_det_res"]["boxes"]
        o = pr["overall_ocr_res"]
        lines = [{"text": t_, "bbox": [float(v) for v in b]} for t_, b in zip(o["rec_texts"], o["rec_boxes"])
                 if any(k in t_ for k in a.text)]
        hits = []
        for ln in lines:
            cover = [{"label": b["label"], "score": round(b["score"], 3), "coordinate": [round(v) for v in b["coordinate"]],
                      "line_inside": round(intersection(ln["bbox"], b["coordinate"]) / area(ln["bbox"]), 3)}
                     for b in boxes if intersection(ln["bbox"], b["coordinate"]) > 0]
            hits.append({"text": ln["text"], "bbox": [round(v) for v in ln["bbox"]], "covering_boxes": cover})
        row = {"layoutThreshold": th, "http_status": resp.status_code, "seconds": sec, "box_count": len(boxes),
               "labels": sorted({b["label"] for b in boxes}), "target_lines": hits}
        summary["probes"].append(row)
        print(th, resp.status_code, sec, "boxes", len(boxes), [(h["text"], [(c["label"], c["score"]) for c in h["covering_boxes"]]) for h in hits])
    dump(a.out / "probe.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
