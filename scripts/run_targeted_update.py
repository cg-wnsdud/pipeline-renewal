"""기존 page-first 실행에 이번 변경(docs/11)으로 새로 생기는 호출만 더해 결과를 다시 만든다(회귀 확인용).

대상은 조건으로 자동 산출한다(영역 ID 지정 없음).
  - 표 후보 전부: table-cells/1(text·셀 구조 한 응답).
  - 1차 page 응답이 지금의 영역 확인 조건(verify_reasons, reading_order_conflict 포함)에 걸리지만 기존 실행에서 영역 확인을
    받지 않은 일반 영역: region-reread/1 조건 B.
기존 호출 기록은 그대로 복사하고(재호출 없음) 새 호출은 다음 번호로 이어 붙인 뒤, 저장 응답만으로 결과·화면을 재구성한다.

사용: run_targeted_update.py --base work/pipeline/page-first-reread-2026-10-26 --out work/pipeline/<새 폴더> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import page_read, table_cells  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.final_output import build_result  # noqa: E402
from layout_pipeline.final_viewer import build_final_viewer  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.read_stage import Reader, reconstruct_reads  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402


def page_results(trace_dir: Path) -> dict[str, tuple[dict, dict, dict]]:
    """region_id → (1차 page 응답 결과, 판독 단위 view, 호출 기록)."""
    out = {}
    for path in sorted((trace_dir / "vlm").glob("*.json")):
        rec = load(path)
        meta = rec["request_meta"]
        if meta.get("kind") != "page":
            continue
        parsed = page_read.validate_page(SimpleNamespace(error=rec["error"], parsed=rec["parsed"]),
                                         {"region_ids": meta["region_ids"], "table_ids": meta.get("table_ids") or []})
        view = {"view_id": meta["view_id"], "crop": meta["view_crop"], "band_of": meta.get("band_of")}
        for rid, res in parsed.items():
            out[rid] = (res, view, rec)
    return out


def plan(records: list[dict], reads: dict, pages: dict) -> list[tuple]:
    work = []
    for rec in records:
        for r in rec["regions"]:
            rid = r["region_id"]
            if rid not in pages:
                continue
            res, view, _ = pages[rid]
            kinds = ((reads.get(rid) or {}).get("trace") or {}).get("kinds") or []
            if r["table_candidate"]:
                why = page_read.verify_reasons(rec, r, view, res)
                work.append((rec, [r], "table_cells", {}, {rid: why or ["table_structure_read"]}, view))
            elif not {"reread", "verify", "patch"} & set(kinds):
                why = page_read.verify_reasons(rec, r, view, res)
                if why:
                    work.append((rec, [r], "reread", {rid: res.get("text")}, {rid: why}, view))
    return work


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--vlm-concurrency", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    base_res = load(a.base / "result.json")
    stage1_dir = Path(base_res["settings"]["stage1"])
    page_ids = [p["page_id"] for p in base_res["pages"]]
    records = [load(stage1_dir / "records" / f"{pid}.json") for pid in page_ids]
    reads = load(a.base / "trace" / "reads.json")
    pages = page_results(a.base / "trace")
    work = plan(records, reads, pages)
    summary = [(w[2], w[1][0]["region_id"], w[4][w[1][0]["region_id"]]) for w in work]
    print(json.dumps({"planned_calls": len(work), "work": summary}, ensure_ascii=False, indent=1))
    if a.dry_run:
        return 0
    if a.out.exists() and any(a.out.iterdir()):
        ap.error(f"출력 폴더가 비어 있지 않습니다: {a.out}")
    (a.out / "trace" / "vlm").mkdir(parents=True)
    last = 0
    for path in sorted((a.base / "trace" / "vlm").glob("*.json")):     # .superseded 는 복사하지 않음(재구성 대상 아님)
        shutil.copy2(path, a.out / "trace" / "vlm" / path.name)
        last = max(last, int(path.stem.rsplit("-", 1)[1]))
    load_env_file()
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")))
    client.count = last
    reader = Reader(client, a.out / "trace", a.vlm_concurrency)
    reader.table_read = "cells"
    images = {rec["page"]["page_id"]: Image.open(rec["page"]["image_path"]).convert("RGB") for rec in records}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.vlm_concurrency) as pool:
        list(pool.map(lambda w: reader.verify_job(w[0], images[w[0]["page"]["page_id"]], w[1], w[2], w[3], w[4], w[5]), work))
    wall = round(time.perf_counter() - t0, 3)
    reads2 = reconstruct_reads(a.out / "trace", records)
    settings = base_res["settings"] | {"table_read": table_cells.VERSION, "targeted_update_of": str(a.base),
                                        "verify_conditions": (base_res["settings"].get("verify_conditions") or {})
                                        | {"reading_order": page_read.ORDER_CHECK_VERSION}}
    result, regions = build_result(records, reads2, settings)
    dump(a.out / "result.json", result)
    dump(a.out / "trace" / "reads.json", {r["region_id"]: {k: r.get(k) for k in ("trace", "contract_errors", "uncertain", "review")}
                                          for r in regions})
    dump(a.out / "trace" / "timing.json", {"new_calls": client.count - last, "reused_calls": last,
                                           "read": {"vlm_concurrency": a.vlm_concurrency, "wall_seconds": wall},
                                           "planned": summary})
    viewer = build_final_viewer(a.out, stage1_dir)
    print(json.dumps({"contract_checks": result["contract_checks"]["passed"], "new_calls": client.count - last,
                      "wall_seconds": wall, "viewer": str(viewer)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
