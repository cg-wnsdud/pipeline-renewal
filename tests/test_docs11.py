"""docs/11 오프라인 테스트: 표 셀 구조 계약·묶음 선택, 순서 확인 조건, Stage 1 집계."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import page_read, table_cells  # noqa: E402
from layout_pipeline.assignment import AssignmentRule  # noqa: E402
from layout_pipeline.vlm_client import VlmCall  # noqa: E402


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "rowspan": rs, "colspan": cs, "text": text}


L008_CELLS = [_cell(0, 0, "공통"), _cell(0, 1, "내용"), _cell(0, 2, "금리"), _cell(1, 0, "본 적금에 가입한 고객", rs=3),
              _cell(1, 1, "급여이체 시"), _cell(1, 2, "0.30%p"), _cell(2, 1, "청약·펀드"), _cell(2, 2, "0.20%p"),
              _cell(3, 1, "카드 실적"), _cell(3, 2, "0.20%p"), _cell(4, 0, "비대면 가입", cs=2), _cell(4, 2, "0.10%p")]


def test_cell_contract_checks_spans_overlap_and_holes():
    assert table_cells.check_cells(True, 5, 3, L008_CELLS) == []
    assert table_cells.check_cells(False, 0, 0, []) == []
    assert table_cells.check_cells(False, 2, 2, [_cell(0, 0, "x")])                      # 표 아님인데 셀
    dup = L008_CELLS + [_cell(2, 0, "본 적금에 가입한 고객")]                                  # 세로 병합 칸에 문구 반복
    assert any("중복" in e for e in table_cells.check_cells(True, 5, 3, dup))
    hole = [c for c in L008_CELLS if c["text"] != "0.10%p"]
    assert any("덮이지 않은" in e for e in table_cells.check_cells(True, 5, 3, hole))
    bad = [dict(c) for c in L008_CELLS]
    bad[3]["rowspan"] = 5                                                                    # 격자 밖
    assert any("범위" in e for e in table_cells.check_cells(True, 5, 3, bad))
    rows = table_cells.to_rows(5, 3, L008_CELLS)
    assert rows[2] == ["", "청약·펀드", "0.20%p"] and rows[4] == ["비대면 가입", "", "0.10%p"]   # 병합 문구는 시작 칸에 한 번


def _table_record(tmp_path):
    img_path = tmp_path / "p.png"
    Image.new("RGB", (400, 300), "white").save(img_path)
    view = {"view_id": "p-v0", "page_id": "p", "kind": "whole", "crop": [0, 0, 400, 300], "width": 400, "height": 300, "aspect": 1.33}
    regions = [{"region_id": f"p-v0-L00{i}", "page_id": "p", "view_id": "p-v0", "bbox": box, "table_candidate": True,
                "source": "prunedResult.layout_det_res.boxes", "source_index": i, "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
               for i, box in enumerate([[10, 10, 390, 200], [10, 220, 390, 290]])]
    asg = [{"region_id": "p-v0-L000", "ocr_id": "p-v0-T000", "ocr_text": "공통 내용 금리", "ocr_bbox": [20, 20, 300, 40]},
           {"region_id": "p-v0-L001", "ocr_id": "p-v0-T001", "ocr_text": "가입기간 금리", "ocr_bbox": [20, 230, 300, 260]}]
    page = {"page_id": "p", "image_path": str(img_path), "submission_id": "s", "file_id": "f", "page_no": 1, "width": 400,
            "height": 300, "image_sha256": "x", "source": {"source_name": "p.png"}}
    return {"page": page, "status": "READY", "route": {"route": "whole", "views": [view], "basis": ""},
            "regions": regions, "assignments": asg, "unassigned_texts": [], "diagnostics": {"region_overlaps": []},
            "issues": [], "ocr_items_total": 2}


def test_table_cells_bundle_replaces_page_rows_and_failure_keeps_page_bundle(tmp_path):
    """table-cells/1: 성공하면 text·cells·rows 를 같은 응답 하나로, 실패하면 1차 page 응답 묶음을 그대로 두고 표시한다."""
    from layout_pipeline.final_output import build_result
    from layout_pipeline.read_stage import Reader, reconstruct_reads
    record = _table_record(tmp_path)
    page_rows = [["공통", "내용", "금리"], ["", "카드 실적 급여이체 시", "0.20%p"]]          # 1차: 행 혼합

    class Fake:
        count = 0

        def chat(self, text, images, schema, name, max_tokens, meta):
            self.count += 1
            if meta["kind"] == "page":
                parsed = {"regions": [{"region_id": "p-v0-L000", "text": "공통 내용 금리", "uncertain": False},
                                      {"region_id": "p-v0-L001", "text": "가입기간 금리", "uncertain": False}],
                          "tables": [{"region_id": "p-v0-L000", "is_table": True, "rows": page_rows},
                                     {"region_id": "p-v0-L001", "is_table": True, "rows": [["가입기간"], ["금리"]]}]}
                return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)
            assert meta["kind"] == "table_cells" and len(images) == 2                      # 표 crop + 문맥
            rid = meta["region_ids"][0]
            if rid == "p-v0-L001":                                                          # 계약 실패(격자 구멍)
                parsed = {"region_id": rid, "is_table": True, "n_rows": 2, "n_cols": 2, "cells": [_cell(0, 0, "가입기간")],
                          "text": "가입기간 금리", "uncertain": False, "notes": ""}
                return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)
            parsed = {"region_id": rid, "is_table": True, "n_rows": 5, "n_cols": 3, "cells": L008_CELLS,
                      "text": "공통 내용 금리\n본 적금에 가입한 고객 급여이체 시 0.30%p", "uncertain": False, "notes": "세로 병합 1"}
            return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)

    reader = Reader(Fake(), tmp_path / "trace", 1)
    reader.table_read = "cells"
    out = reader.read_pages_page_first([record], max_images=8)
    a, b = out["p-v0-L000"], out["p-v0-L001"]
    assert a["cells"] == sorted(L008_CELLS, key=lambda c: (c["row"], c["col"])) and a["rows"][3] == ["", "카드 실적", "0.20%p"]
    assert a["text"].startswith("공통 내용 금리\n본 적금") and a["trace"]["page_rows"] == page_rows   # 1차 rows 는 trace 만
    assert b["rows"] == [["가입기간"], ["금리"]] and b["table_structure_failed"] and b["text"] == "가입기간 금리"
    result, _ = build_result([record], out, {})
    by = {r["region_id"]: r for r in result["regions"]}
    assert by["p-v0-L000"]["table"]["structure"] == table_cells.VERSION and by["p-v0-L000"]["table"]["cells"]
    assert "table_structure_failed" in by["p-v0-L001"]["review"]["quality"]
    again = reconstruct_reads(tmp_path / "trace", [record])
    assert again["p-v0-L000"]["cells"] == a["cells"] and again["p-v0-L001"]["rows"] == b["rows"]


def test_legacy_reread_of_table_text_is_flagged_as_mixed_reads():
    from layout_pipeline.read_stage import integrate_region
    region = {"region_id": "p-v0-L000", "page_id": "p", "bbox": [0, 0, 10, 10], "source": "s", "source_index": 0,
              "table_candidate": True, "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
    record = {"assignments": [], "unassigned_texts": [], "diagnostics": {"region_overlaps": []}}
    res = {"text": "고친 문구", "is_table": True, "rows": [["옛 행"]], "contract_ok": True,
           "trace": {"kinds": ["page", "reread"], "page_text": "1차 문구"}}
    assert "table_text_rows_from_different_reads" in integrate_region(record, region, res)["review"]["quality"]


def _ocr(i, text, box):
    return {"ocr_id": f"p-v0-T{i:03d}", "region_id": "p-v0-L000", "ocr_text": text, "ocr_bbox": box}


def test_order_conflict_only_for_simple_columns():
    region = {"region_id": "p-v0-L000", "bbox": [0, 0, 600, 300], "table_candidate": False}
    stacked = {"assignments": [_ocr(0, "중도해지 및", [10, 10, 150, 40]), _ocr(1, "만기후 금리", [10, 50, 150, 80])],
               "unassigned_texts": []}
    assert page_read.order_conflict(stacked, region, "만기후 금리\n중도해지 및")["inversions"] == 1
    assert page_read.order_conflict(stacked, region, "중도해지 및\n만기후 금리") is None
    # 같은 줄 조각(가까움)은 한 줄: 제목 한 줄 + 아래 줄
    frag = {"assignments": [_ocr(0, "이자에", [10, 10, 60, 40]), _ocr(1, "이자가 붙어", [70, 10, 200, 40]),
                            _ocr(2, "NH직장인", [10, 60, 200, 120])], "unassigned_texts": []}
    assert page_read.order_conflict(frag, region, "NH직장인\n이자에 이자가 붙어") is not None
    # 좌우로 멀리 떨어진 항목–값(단이 나뉜 배치)은 검사하지 않는다
    side = {"assignments": [_ocr(0, "가입대상", [10, 10, 80, 40]), _ocr(1, "만 18세 이상", [400, 10, 590, 40]),
                            _ocr(2, "가입기간", [10, 60, 80, 90]), _ocr(3, "1년 ~3년", [400, 60, 590, 90])], "unassigned_texts": []}
    assert page_read.simple_column_lines(side["assignments"]) is None
    assert page_read.order_conflict(side, region, "만 18세 이상 1년 ~3년 가입대상 가입기간") is None
    table = dict(region, table_candidate=True)
    assert page_read.order_conflict(stacked, table, "만기후 금리\n중도해지 및") is None


def test_stage1_report_separates_scout_calls_and_counts_candidate_run_as_executed():
    from layout_pipeline.report import build_report

    def call(cid, vid, kind, boxes, items):
        return {"call_id": cid, "page_id": "p", "view_id": vid, "view_kind": kind, "ok": True, "seconds": 0.5,
                "input_crop": [0, 0, 10, 10], "sent_sha256": "x", "response_size_matches": True,
                "counts": {"layout_det_boxes": boxes, "ocr_items": items}}
    rec = {"page": {"page_id": "p", "source": {"kind": "image"}, "width": 600, "height": 3000,
                    "geometry": {"aspect": 5.0, "orientation": "portrait"}, "image_sha256": "y"},
           "route": {"shape": "long", "route": "long"}, "status": "CANDIDATE_RUN", "quality": {"status": "candidate_unvalidated"},
           "regions": [{"table_candidate": False}] * 3, "server_blocks": [], "ocr_items_total": 5,
           "assignments": [{"empty_text": False, "exact_inside": True}] * 3,
           "unassigned_texts": [{"empty_text": False, "reason": "partial_overlap"}] * 2,
           "partition_checks": {"passed": True}, "diagnostics": {"region_overlaps": [], "regions_without_text": []},
           "gold": None, "vlm_plan": {"checks": {"read_text": True, "table_check": True}}, "issues": []}
    calls = [call("ocr-1", "p-s0", "scout", 9, 40), call("ocr-2", "p-t0", "candidate_tile", 2, 3),
             call("ocr-3", "p-t1", "candidate_tile", 1, 2)]
    meta = {"assignment_rule": AssignmentRule().to_dict(), "routing_policy": {"long_aspect_min": 3.0, "status": "provisional"}}
    rep = build_report([rec], calls, meta)
    t = rep["totals"]
    assert t["raw_to_output"]["match"] and t["final_ocr_calls"] == 2 and t["scout_ocr_calls"] == 1
    assert t["executed_pages"] == 1 and t["pending_pages"] == [] and t["candidate_run_pages"] == 1
    assert rep["status_table"][2]["state"].startswith("미측정")


def test_position_candidate_order_and_prompt_wording(monkeypatch, tmp_path):
    """docs/18: position 이면 OCR 후보를 위→아래(같은 줄은 왼쪽→오른쪽)로 주고 프롬프트의 '문자열 순' 설명을 모두 바꾼다."""
    from layout_pipeline import vlm_tasks
    region = {"region_id": "p-v0-L000", "page_id": "p", "view_id": "p-v0", "bbox": [0, 0, 400, 200], "table_candidate": True,
              "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
    lines = [("p-v0-T000", "※ 각주", [10, 120, 200, 140]), ("p-v0-T001", "(직전 분기)", [10, 150, 200, 170]),
             ("p-v0-T002", "가입기간", [10, 10, 100, 30]), ("p-v0-T003", "12개월", [150, 12, 250, 32])]
    record = {"page": {"page_id": "p", "image_path": ""}, "regions": [region], "unassigned_texts": [],
              "route": {"views": [{"view_id": "p-v0", "crop": [0, 0, 400, 200]}]}, "diagnostics": {"region_overlaps": []},
              "assignments": [{"region_id": region["region_id"], "ocr_id": i, "ocr_text": t, "ocr_bbox": b} for i, t, b in lines]}
    assert [c["text"] for c in vlm_tasks.candidate_context(record, region, "crop/3")["ocr_candidates"]][0] == "(직전 분기)"
    monkeypatch.setattr(vlm_tasks, "CANDIDATE_ORDER", "position")
    assert [c["text"] for c in vlm_tasks.candidate_context(record, region, "crop/3")["ocr_candidates"]] == \
        ["가입기간", "12개월", "※ 각주", "(직전 분기)"]
    img = Image.new("RGB", (400, 200), "white")
    texts = [page_read.page_request(record, record["route"]["views"][0], img)["text"],
             page_read.reread_request(record, region, img, "가입기간", record["route"]["views"][0])["text"],
             table_cells.request(record, region, img, record["route"]["views"][0])["text"],
             vlm_tasks.crop_request(record, [region], img, 0, "crop/3")["text"]]
    for t in texts:
        out = vlm_tasks.order_prompt(t)
        assert "문자열 순" not in out and "순서 없음" not in out and "위치 순" in out


def test_table_text_fallback_uses_cells_read_text_without_rows():
    """docs/18: 표 셀 재판독이 셀 구조만 실패하면, text_fallback 일 때 그 문구를 쓰고 1차 판독 rows 와 섞지 않는다."""
    from types import SimpleNamespace
    from layout_pipeline.read_stage import merge_entries
    rid = "p-v0-L002"
    page = {"request_meta": {"kind": "page"}, "call_id": "vlm-0001", "finish_reason": "stop"}
    first = {rid: {"region_id": rid, "text": "(직전 분기)\n표\n(직전 분기)", "uncertain": False, "is_table": True,
                   "rows": [["표"]], "contract_ok": True, "contract_errors": []}}
    bad = {"region_id": rid, "is_table": True, "text": "표\n(직전 분기)", "uncertain": False, "n_rows": 1, "n_cols": 1,
           "cells": [{"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "표"}]}
    req = {"region_ids": [rid]}
    parsed = table_cells.validate(SimpleNamespace(error=None, parsed=bad), req)
    assert not parsed[rid]["contract_ok"] and parsed[rid]["text_only"]["text"] == "표\n(직전 분기)"
    for flag, want_text, want_rows in ((False, first[rid]["text"], [["표"]]), (True, "표\n(직전 분기)", None)):
        cells = {"request_meta": {"kind": "table_cells", "text_fallback": flag}, "call_id": "vlm-0002", "finish_reason": "stop"}
        out = merge_entries([(page, first), (cells, parsed)])[rid]
        assert out["text"] == want_text and out["rows"] == want_rows and out["table_structure_failed"]


def test_cells_read_that_is_not_a_table_keeps_the_first_read_line_breaks():
    """셀 재판독이 '표 아님'이면 문구 글자는 재판독 그대로 쓰고 줄바꿈 위치는 1차 판독에서 옮긴다(2026-10-29 dm05 유의사항)."""
    from types import SimpleNamespace
    from layout_pipeline.read_stage import merge_entries
    from layout_pipeline.text_eval import norm
    rid = "p-v0-L003"
    page = {"request_meta": {"kind": "page"}, "call_id": "vlm-0001", "finish_reason": "stop"}
    first = {rid: {"region_id": rid, "text": "거래중지계좌 다음에 해당하는 계좌\n1. 잔액 1만원 미만인 계좌\n2. 잔액 5만원 미만인 계좌\n"
                                               "확인 후 가능합니다\n※ 약관을 읽어보시기 바랍니다",
                   "uncertain": False, "is_table": True, "rows": [], "contract_ok": True, "contract_errors": []}}
    joined = ("거래중지계좌 다음에 해당하는 계좌 · 1. 잔액 1만원 미만인 계좌 · 2. 잔액 5만원 미만인 계좌 "
              "확인 후 가능합니다. ※ 약관을 읽어보시기 바랍니다.")
    cells = {"request_meta": {"kind": "table_cells"}, "call_id": "vlm-0002", "finish_reason": "stop"}
    for is_table, want in ((False, "거래중지계좌 다음에 해당하는 계좌\n· 1. 잔액 1만원 미만인 계좌\n· 2. 잔액 5만원 미만인 계좌\n"
                                   "확인 후 가능합니다.\n※ 약관을 읽어보시기 바랍니다."), (True, joined)):
        resp = {"region_id": rid, "is_table": is_table, "text": joined, "uncertain": False,
                "n_rows": 1 if is_table else 0, "n_cols": 1 if is_table else 0,
                "cells": [_cell(0, 0, joined)] if is_table else []}
        parsed = table_cells.validate(SimpleNamespace(error=None, parsed=resp), {"region_ids": [rid]})
        out = merge_entries([(page, first), (cells, parsed)])[rid]
        assert out["text"] == want and norm(out["text"]) == norm(joined)        # 글자는 재판독 그대로
        assert out["trace"].get("line_breaks_from_first_read") == (4 if is_table is False else None)
    assert table_cells.with_line_breaks("가나다라\n마바사아", "가나다라마바사아") == ("가나다라마바사아", 0)   # 공백 없는 자리
