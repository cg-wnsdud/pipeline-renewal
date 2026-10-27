"""공유 OCR 서버의 클래스별 레이아웃 옵션(dict) 처리 상태를 읽기 전용 요청으로 확인한다(서버 수정·재시작 없음).

배경(서버 조사 2026-10-25): 요청 JSON 의 dict 키는 문자열인데 후처리는 정수 키로 조회해, 클래스별 layoutThreshold 는
모두 0.5 로, 클래스별 layoutMergeBboxesMode 는 '필터 없음(union)' 으로 처리된다. 서버 래퍼에서 키를 정수로 바꾸고
YAML 값과 병합하는 패치가 제안됐다. 이 스크립트는 패치가 적용됐는지를 같은 페이지에 대한 요청 8회로 판정한다.

판정(같은 이미지, 박스 목록 = (label, 반올림 좌표) 집합):
- 미적용: {"2":0.4} 결과 == 숫자 0.5 결과, {"abc":0.7} 이 HTTP 200, {"0":"large"} 결과 == "union" 결과
- 적용:   {"2":0.4} 결과 == 기본 결과(텍스트 YAML 값 0.4 와 같은 의도), {"abc":0.7}·{"2":1.5} 가 4xx,
          {"0":"large"} 결과 == 기본 결과
그 밖의 조합은 '판정 불가'로 남긴다.

사용: probe_layout_options.py --page-image <png> --out work/server-probe/<이름>
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
from layout_pipeline.jsonio import dump  # noqa: E402
from layout_pipeline.ocr_client import BASE_OPTIONS  # noqa: E402

VARIANTS = [
    ("base", {}),
    ("threshold_0.5", {"layoutThreshold": 0.5}),
    ("threshold_dict_text_0.4", {"layoutThreshold": {"2": 0.4}}),
    ("threshold_dict_bad_key", {"layoutThreshold": {"abc": 0.7}}),
    ("threshold_dict_bad_value", {"layoutThreshold": {"2": 1.5}}),
    ("merge_union", {"layoutMergeBboxesMode": "union"}),
    ("merge_dict_0_large", {"layoutMergeBboxesMode": {"0": "large"}}),
    ("merge_large", {"layoutMergeBboxesMode": "large"}),
]


def boxes_key(pruned: dict) -> list:
    return sorted((b["label"], *[round(v) for v in b["coordinate"]]) for b in pruned["layout_det_res"]["boxes"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page-image", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    load_env_file()
    url, timeout = os.environ["PN_OCR_URL"], float(os.environ.get("PN_OCR_TIMEOUT_S", "300"))
    blob = base64.b64encode(a.page_image.read_bytes()).decode("ascii")
    rows = {}
    for name, opts in VARIANTS:
        t = time.perf_counter()
        try:
            resp = requests.post(url, json={"fileType": 1, "file": blob, **BASE_OPTIONS, **opts}, timeout=(10, timeout))
            status = resp.status_code
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None
        except requests.RequestException as exc:
            status, data = None, {"error": f"{type(exc).__name__}: {exc}"}
        row = {"options": opts, "http_status": status, "seconds": round(time.perf_counter() - t, 2)}
        if status == 200 and data and data.get("errorCode") in (0, None):
            pruned = data["result"]["layoutParsingResults"][0]["prunedResult"]
            row["boxes"] = boxes_key(pruned)
            row["box_count"] = len(row["boxes"])
        else:
            row["error"] = (data or {}).get("errorMsg") or (data or {}).get("detail") or (data or {}).get("error")
        rows[name] = row
        print(name, status, row.get("box_count"), row.get("error") or "")

    def same(x: str, y: str) -> bool | None:
        bx, by = rows[x].get("boxes"), rows[y].get("boxes")
        return None if bx is None or by is None else bx == by

    ok4xx = lambda n: rows[n]["http_status"] is not None and 400 <= rows[n]["http_status"] < 500  # noqa: E731
    unpatched = (same("threshold_dict_text_0.4", "threshold_0.5") and rows["threshold_dict_bad_key"]["http_status"] == 200
                 and same("merge_dict_0_large", "merge_union"))
    patched = (same("threshold_dict_text_0.4", "base") and ok4xx("threshold_dict_bad_key") and ok4xx("threshold_dict_bad_value")
               and same("merge_dict_0_large", "base"))
    verdict = "patch_applied" if patched else ("patch_not_applied" if unpatched else "undetermined")
    summary = {"verdict": verdict, "page_image": str(a.page_image), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "comparisons": {"dict_text_0.4==base": same("threshold_dict_text_0.4", "base"),
                               "dict_text_0.4==number_0.5": same("threshold_dict_text_0.4", "threshold_0.5"),
                               "merge_dict_0_large==base": same("merge_dict_0_large", "base"),
                               "merge_dict_0_large==union": same("merge_dict_0_large", "merge_union")},
               "requests": rows,
               "statement": "읽기 전용 요청만 보냈다. 서버 설정·코드·컨테이너는 바꾸지 않았다."}
    dump(a.out / "probe.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "requests"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
