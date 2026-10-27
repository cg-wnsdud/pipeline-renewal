"""docs/12 파일별 결과(layout-result/2) 오프라인 테스트: 분리·보존·계약·참조 규칙·mock 사례."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import contract_v2  # noqa: E402
from layout_pipeline.file_result import NOT_RUN_ASSIGNMENT, NOT_RUN_LABELING, build_file_results, write_file_results  # noqa: E402
from layout_pipeline.final_output import build_result  # noqa: E402
from layout_pipeline.jsonio import dump  # noqa: E402

MOCK = json.loads((ROOT / "fixtures" / "contract-mocks" / "submission-two-files.json").read_text(encoding="utf-8"))


def _page_record(tmp, sub, fid, page_no, src_name, kind, sha, regions, unassigned=(), status="READY"):
    pid = f"{fid}_p{page_no:02d}"
    img = tmp / "stage1" / "pages" / f"{pid}.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (100, 100), "white").save(img)
    view = {"view_id": f"{pid}-v0", "page_id": pid, "kind": "whole", "crop": [0, 0, 100, 100], "width": 100, "height": 100, "aspect": 1.0}
    regs = [{"region_id": f"{pid}-v0-L{i:03d}", "page_id": pid, "view_id": f"{pid}-v0", "bbox": b, "table_candidate": t,
             "source": "prunedResult.layout_det_res.boxes", "source_index": i, "raw_label": "text",
             "crossing_ocr_ids": [], "ambiguous_ocr_ids": []} for i, (b, t) in enumerate(regions)]
    un = [{"ocr_id": f"{pid}-v0-T{i:03d}", "page_id": pid, "ocr_text": txt, "ocr_bbox": b, "score": 0.9, "reason": "outside_layout",
           "candidates": [], "empty_text": False, "source": "prunedResult.overall_ocr_res"} for i, (txt, b) in enumerate(unassigned)]
    return {"page": {"page_id": pid, "submission_id": sub, "file_id": fid, "page_no": page_no, "width": 100, "height": 100,
                     "image_path": str(img), "image_sha256": "a" * 64,
                     "source": {"kind": kind, "source_name": src_name, "source_sha256": sha, "page_count": 2 if kind == "pdf" else 1}},
            "route": {"route": "whole", "views": [view], "basis": "", "boundary_plan": None}, "status": status,
            "regions": regs, "assignments": [], "unassigned_texts": un, "ocr_items_total": len(un),
            "diagnostics": {"region_overlaps": []},
            "issues": [{"code": "GOLD_LINES_NOT_COVERED", "severity": "review", "message": "평가용"},
                       {"code": "TEXT_OUTSIDE_LAYOUT", "severity": "review", "message": "밖"}]}


def _run(tmp):
    """같은 제출 묶음: 2쪽 PDF(같은 원본 파일명 두 개 중 하나) + 이미지. 두 파일의 원본 파일명이 같아도 file_id 로 분리."""
    recs = [_page_record(tmp, "s1", "a01", 1, "광고.pdf", "pdf", "1" * 64, [([0, 0, 50, 50], False), ([0, 60, 90, 90], True)],
                         [("밖 문구", [60, 0, 90, 10])]),
            _page_record(tmp, "s1", "a01", 2, "광고.pdf", "pdf", "1" * 64, [([0, 0, 50, 50], False)]),
            _page_record(tmp, "s1", "a02", 1, "광고.pdf", "image", "2" * 64, [([0, 0, 50, 50], False)])]
    s1 = tmp / "stage1"
    dump(s1 / "run.json", {"routing_policy": {"long_aspect_min": 3.0, "status": "provisional"}, "pdf_dpi": 200,
                           "assignment_rule": {"name": "core-containment/1", "core_shrink": 0.0}})
    dump(s1 / "calls.json", [])
    for r in recs:
        dump(s1 / "records" / f"{r['page']['page_id']}.json", r)
    reads = {"a01_p01-v0-L000": {"text": "본문", "uncertain": False, "contract_ok": True, "contract_errors": [],
                                 "trace": {"calls": ["vlm-0001"], "kinds": ["page"]}},
             "a01_p01-v0-L001": {"text": "가입기간 금리", "uncertain": False, "contract_ok": True, "contract_errors": [],
                                 "is_table": True, "rows": [["가입기간", "금리"]], "n_rows": 1, "n_cols": 2,
                                 "cells": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "가입기간"},
                                           {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "금리"}],
                                 "trace": {"calls": ["vlm-0001", "vlm-0002"], "kinds": ["page", "table_cells"],
                                           "table_read": "table-cells/1"}},
             "a01_p02-v0-L000": {"text": "", "uncertain": False, "contract_ok": True, "contract_errors": [],
                                 "trace": {"calls": ["vlm-0003"], "kinds": ["page"]}},
             "a02_p01-v0-L000": {"text": None, "uncertain": None, "contract_ok": False, "contract_errors": ["HTTP 500"],
                                 "trace": {"calls": ["vlm-0004"], "kinds": ["page"]}}}
    settings = {"stage1": str(s1), "read_mode": "page-first", "page_request": "page/2", "verify_request": "region-reread/1",
                "table_read": "table-cells/1", "table_request": "table-check/3", "temperature": 0,
                "assignment_rule": {"name": "core-containment/1", "core_shrink": 0.0}}
    v1, _ = build_result(recs, reads, settings)
    (tmp / "trace" / "vlm").mkdir(parents=True)
    return recs, v1, s1


def test_unassigned_output_keeps_only_text_outside_regions(tmp_path):
    """unassigned-output/1: 엄격 배정에서 미배정이어도 면적 50% 이상이 한 영역 안인 OCR 줄은 결과의 미배정에 넣지 않는다.
    Stage 1 기록(VLM 입력 근거)은 그대로 둔다."""
    rec = _page_record(tmp_path, "s1", "a01", 1, "광고.pdf", "pdf", "1" * 64, [([0, 0, 50, 50], False)],
                       [("영역 안(경계 0.4px 돌출)", [1, 1, 50.4, 10]), ("반쯤 걸침", [40, 20, 60, 30]),
                        ("거의 밖", [45, 40, 95, 45]), ("영역 밖", [60, 60, 90, 70])])
    cands = [[{"region_id": "a01_p01-v0-L000", "inside_ratio": 0.99}], [{"region_id": "a01_p01-v0-L000", "inside_ratio": 0.5}],
             [{"region_id": "a01_p01-v0-L000", "inside_ratio": 0.1}], []]
    for u, c, reason in zip(rec["unassigned_texts"], cands, ["partial_overlap"] * 3 + ["outside_layout"]):
        u.update(candidates=c, reason=reason)
    before = copy.deepcopy(rec["unassigned_texts"])
    reads = {"a01_p01-v0-L000": {"text": "영역 안", "uncertain": False, "contract_ok": True, "contract_errors": [],
                                 "trace": {"calls": ["vlm-0001"], "kinds": ["page"]}}}
    v1, _ = build_result([rec], reads, {"read_mode": "page-first"})
    assert [u["ocr_text"] for u in v1["unassigned_texts"]] == ["거의 밖", "영역 밖"]
    assert v1["pages"][0]["unassigned_texts"] == 2 and v1["pages"][0]["ocr_in_region_by_overlap"] == 2
    assert v1["contract_checks"]["passed"] and rec["unassigned_texts"] == before
    assert v1["settings"]["unassigned_output"]["name"] == "unassigned-output/1"


def test_file_split_preserves_values_and_keeps_multi_page_file_together(tmp_path):
    recs, v1, s1 = _run(tmp_path)
    docs = build_file_results(ROOT, tmp_path, v1, recs, s1, tmp_path / "trace")
    assert [d["source"]["file_id"] for d in docs] == ["a01", "a02"]
    a, b = docs
    assert [p["page_id"] for p in a["pages"]] == ["a01_p01", "a01_p02"] and a["source"]["page_count"] == 2   # 2쪽 PDF = 문서 1개
    assert (a["source"]["input_order"], b["source"]["input_order"]) == (1, 2)
    assert a["source"]["file_name"] == b["source"]["file_name"] == "광고.pdf"                                  # 같은 파일명도 file_id 로 분리
    by = {r["region_id"]: r for d in docs for r in d["regions"]}
    v1r = {r["region_id"]: r for r in v1["regions"]}
    for rid, r in by.items():
        assert (r["bbox"], r["text"], r["text_status"], r["table"]) == (v1r[rid]["bbox"], v1r[rid]["text"], v1r[rid]["text_status"], v1r[rid]["table"])
        assert r["product_assignment"] == NOT_RUN_ASSIGNMENT and r["labeling"] == NOT_RUN_LABELING
    assert by["a01_p02-v0-L000"]["text"] == "" and by["a01_p02-v0-L000"]["text_status"] == "no_text"            # 빈 문구
    assert by["a02_p01-v0-L000"]["text"] is None and by["a02_p01-v0-L000"]["text_status"] == "read_failed"      # 판독 실패
    assert by["a01_p01-v0-L001"]["table"]["cells"] and by["a01_p01-v0-L001"]["table"]["structure"] == "table-cells/1"
    assert a["unassigned_texts"][0]["final"] is False and a["unassigned_texts"][0]["ocr_text"] == "밖 문구"
    assert a["products"] == [] and all(not i["code"].startswith("GOLD_") for p in a["pages"] for i in p["issues"])
    assert any(i["code"] == "REGION_READ_FAILED" for i in b["pages"][0]["issues"])
    for d in docs:
        assert contract_v2.check_document(d) == []
    assert contract_v2.check_submission(docs) == []
    again = build_file_results(ROOT, tmp_path, v1, recs, s1, tmp_path / "trace")
    assert [d["result_id"] for d in again] == [d["result_id"] for d in docs]                                   # 재구성 시 유지
    index = write_file_results(tmp_path, docs)
    assert [i["result"] for i in index] == ["files/a01/result.json", "files/a02/result.json"]
    with pytest.raises(ValueError):
        write_file_results(tmp_path, [docs[0], copy.deepcopy(docs[0])])                                          # 덮어쓰기 방지


def test_mock_products_shared_specific_unknown_and_labels_pass_contract():
    docs = MOCK["documents"]
    for d in docs:
        assert contract_v2.check_document(d) == []
    assert contract_v2.check_submission(docs) == []
    regs = {r["region_id"]: r for d in docs for r in d["regions"]}
    assert regs["m01_p01-v0-L000"]["product_assignment"]["product_ids"] == ["P-A", "P-B"]          # 복수 상품(공통)
    assert {d["source"]["file_id"] for d in docs if any("P-A" in r["product_assignment"]["product_ids"] for r in d["regions"])} == {"m01", "m02"}
    assert regs["m01_p02-v0-L000"]["product_assignment"]["scope"] == "unknown"                     # 미확정


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["regions"][0]["product_assignment"]["product_ids"].append("P-X"), "products 에 없는"),
    (lambda d: d["regions"][1]["labeling"]["items"].append({"product_id": "P-B", "label_ids": []}), "영역 product_ids 에 없음"),
    (lambda d: d["regions"][3]["product_assignment"].update(product_ids=["P-A"]), "schema"),                # not_run 인데 값
    (lambda d: d["regions"][3]["labeling"].update(items=[{"product_id": None, "label_ids": ["x"]}]), "schema"),
    (lambda d: d["regions"][0]["product_assignment"].update(scope="specific", product_ids=[]), "비어 있음"),
    (lambda d: d["regions"][3].update(text="x"), "schema"),                                                  # 실패인데 문구
    (lambda d: d["regions"][0].update(table={"status": "table", "rows": []}), "schema"),                     # 표 후보 아님인데 표
    (lambda d: d["unassigned_texts"][0].update(final=True), "schema"),
])
def test_contract_rejects_reference_and_state_violations(mutate, expect):
    d = copy.deepcopy(MOCK["documents"][0])
    mutate(d)
    errs = contract_v2.check_document(d)
    assert errs and any(expect in e for e in errs), errs


def test_same_product_id_with_different_names_in_submission_is_rejected():
    docs = copy.deepcopy(MOCK["documents"])
    docs[1]["products"][0]["name"] = "다른 이름"
    assert any("서로 다른 이름" in e for e in contract_v2.check_submission(docs))
