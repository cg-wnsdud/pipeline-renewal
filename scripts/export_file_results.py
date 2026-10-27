"""기존 실행(layout-result/1)을 입력 파일별 layout-result/2 로 내보내고 보존을 검사한다(모델 호출 없음, docs/12).

원래 실행 폴더는 읽기만 하고, 결과는 --out 새 폴더에 쓴다.
  <out>/files/<file_id>/result.json, <out>/files/index.json(목록), <out>/files/index.html(대조 화면), <out>/export-check.json

보존 검사(분리 전 실행 결과 ↔ 분리 후 파일별 문서 전체):
  페이지·영역·미배정 OCR 수, 원래 ID·bbox, 최종 문구·판독 상태, 표 결과·상태, 검토 사유, 입력 순서·file_id 유일,
  계약(JSON Schema + 참조 규칙), 실패와 빈 문구 구분, 상품·라벨 미실행 초기값.

사용: export_file_results.py --run work/pipeline/page-first-docs11-regression-2026-10-26 --out work/export/<이름>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import contract_v2  # noqa: E402
from layout_pipeline.file_result import NOT_RUN_ASSIGNMENT, NOT_RUN_LABELING, export_run  # noqa: E402
from layout_pipeline.final_viewer import build_file_viewer  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402


def preservation(v1: dict, docs: list[dict]) -> dict:
    """분리 전 실행 결과와 분리 후 파일별 문서의 값이 같은지(바뀐 항목 목록)."""
    regs = {r["region_id"]: r for d in docs for r in d["regions"]}
    uns = {u["ocr_id"]: u for d in docs for u in d["unassigned_texts"]}
    pages = [p["page_id"] for d in docs for p in d["pages"]]
    diffs = []
    if sorted(pages) != sorted(p["page_id"] for p in v1["pages"]) or len(set(pages)) != len(pages):
        diffs.append("페이지 집합 다름")
    if len(regs) != len(v1["regions"]) or sum(len(d["regions"]) for d in docs) != len(v1["regions"]):
        diffs.append("영역 수 다름")
    if len(uns) != len(v1["unassigned_texts"]):
        diffs.append("미배정 OCR 수 다름")
    for r in v1["regions"]:
        x = regs.get(r["region_id"])
        if x is None:
            diffs.append(f"{r['region_id']}: 영역 없음")
            continue
        same = (x["bbox"] == r["bbox"] and x["page_id"] == r["page_id"] and x["text"] == r["text"]
                and x["text_status"] == r["text_status"] and x["table"] == r["table"]
                and x["table_candidate"] == r["table_candidate"]
                and x["review"]["required"] == r["needs_review"] and x["review"]["quality_reasons"] == r["review"]["quality"]
                and x["review"]["context_reasons"] == r["review"]["context"]
                and x["candidate_boundary"] == bool(r.get("candidate_boundary")))
        if not same:
            diffs.append(f"{r['region_id']}: 값 다름")
        if x["product_assignment"] != NOT_RUN_ASSIGNMENT or x["labeling"] != NOT_RUN_LABELING:
            diffs.append(f"{r['region_id']}: 상품·라벨 초기값 아님")
    for u in v1["unassigned_texts"]:
        x = uns.get(u["ocr_id"])
        if x is None or (x["bbox"], x["ocr_text"], x["reason"], x["candidate_region_ids"], x["page_id"]) != \
                (u["ocr_bbox"], u["ocr_text"], u["reason"], u["candidate_region_ids"], u["page_id"]) or x["final"] is not False:
            diffs.append(f"{u['ocr_id']}: 미배정 OCR 값 다름")
    status = {}
    for d in docs:
        for r in d["regions"]:
            status[r["text_status"]] = status.get(r["text_status"], 0) + 1
    empty_vs_failed = all((r["text"] == "") == (r["text_status"] == "no_text") and
                          (r["text"] is None) == (r["text_status"] in ("read_failed", "not_read"))
                          for d in docs for r in d["regions"])
    return {"pages": len(pages), "regions": len(regs), "unassigned_texts": len(uns), "files": len(docs),
            "text_status": status, "empty_vs_failed_consistent": empty_vs_failed, "diffs": diffs, "passed": not diffs}


def run(run_dir: Path, out: Path) -> dict:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"출력 폴더가 비어 있지 않습니다: {out}")
    index = export_run(ROOT, run_dir, out)
    docs = [load(out / i["result"]) for i in index]
    contract = {d["source"]["file_id"]: contract_v2.check_document(d) for d in docs}
    sub = contract_v2.check_submission(docs)
    check = {"run": str(run_dir), "export": str(out), "preservation": preservation(load(run_dir / "result.json"), docs),
             "contract": {"documents_passed": sum(not v for v in contract.values()), "documents": len(docs),
                          "errors": {k: v for k, v in contract.items() if v}, "submission_errors": sub},
             "model_calls": 0}
    check["passed"] = check["preservation"]["passed"] and not check["contract"]["errors"] and not sub
    dump(out / "export-check.json", check)
    build_file_viewer(out, docs, {"passed": check["passed"]})
    return check


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    check = run(a.run, a.out)
    print(json.dumps({k: v for k, v in check.items()}, ensure_ascii=False, indent=1)[:3000])
    return 0 if check["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
