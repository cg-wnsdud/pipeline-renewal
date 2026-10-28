"""놓친 영역 보충(supplement/1, docs/16 §9). 모델·OCR 호출 없음(가짜 판독기, 표 상자 조회 없음).

합성 기록은 단계·분기 확인용이다. 실제 검출·판독 품질 평가가 아니다.
"""
import copy

from PIL import Image

from layout_pipeline import supplement as S
from layout_pipeline.jsonio import load


def U(i, text, bbox, reason="outside_layout", cands=()):
    return {"ocr_id": f"p-v0-T{i:03d}", "source": "prunedResult.overall_ocr_res", "source_index": i, "page_id": "p", "view_id": "p-v0",
            "ocr_text": text, "ocr_bbox": bbox, "score": 0.99, "reason": reason,
            "candidates": [{"region_id": r, "inside_ratio": x} for r, x in cands], "empty_text": False, "final": False}


def record(tmp):
    img = tmp / "p.png"
    Image.new("RGB", (1000, 800), "white").save(img)
    regions = [{"region_id": "p-v0-L000", "page_id": "p", "view_id": "p-v0", "bbox": [0, 0, 1000, 200], "table_candidate": False,
                "source": "prunedResult.layout_det_res.boxes", "source_index": 0},
               {"region_id": "p-v0-L001", "page_id": "p", "view_id": "p-v0", "bbox": [0, 600, 1000, 800], "table_candidate": False,
                "source": "prunedResult.layout_det_res.boxes", "source_index": 1}]
    un = [U(1, "기간", [100, 300, 160, 330]), U(2, "12개월", [300, 300, 380, 330]),          # 2×2 표(격자)
          U(3, "적용이자율", [100, 340, 220, 370]), U(4, "2.60%", [300, 340, 380, 370]),
          U(5, "농협은행이 777명에게 쓴다", [500, 545, 900, 585]),                         # 놓친 제목 한 줄(L001 바로 위)
          U(6, "이 예금은 예금자보호법에 따라 보호됩니다", [0, 590, 900, 610], "partial_overlap", [("p-v0-L001", 0.4)]),  # 이웃 영역 문구에 이미 있음
          U(7, "×", [950, 300, 970, 320])]                                                 # 한 글자 기호
    return {"page": {"page_id": "p", "width": 1000, "height": 800, "image_path": str(img)}, "status": "READY",
            "route": {"views": [{"view_id": "p-v0", "crop": [0, 0, 1000, 800]}]}, "regions": regions,
            "assignments": [{"ocr_id": "p-v0-T000", "region_id": "p-v0-L000", "ocr_text": "NH고향사랑기부 예·적금", "ocr_bbox": [10, 10, 300, 40]}],
            "unassigned_texts": un, "diagnostics": {"region_overlaps": []}, "ocr_items_total": 8,
            "partition_checks": {"ocr_items": 8, "assigned": 1, "unassigned": 7}}


READS = {"p-v0-L000": {"text": "NH고향사랑기부 예·적금"}, "p-v0-L001": {"text": "이 예금은 예금자보호법에 따라 보호됩니다\n농협은행"}}


class FakeReader:
    concurrency = 1

    def __init__(self, answers, table_fails=False):
        self.answers, self.jobs, self.table_fails = answers, [], table_fails

    def run_job(self, rec, img, job):
        self.jobs.append((job["kind"], [r["region_id"] for r in job["regions"]]))
        out = {}
        for r in job["regions"]:
            ocr = " ".join(a["ocr_text"] for a in rec["assignments"] if a["region_id"] == r["region_id"])
            text = self.answers.get(ocr, ocr)
            res = {"text": text, "uncertain": False, "contract_ok": True, "contract_errors": []}
            if job["kind"] == "table" and self.table_fails:      # 격자 밖 빈 셀 같은 구조 검사 실패(문구는 버려짐)
                res = {"text": None, "uncertain": None, "contract_ok": False, "contract_errors": ["셀 범위 밖"]}
            elif job["kind"] == "table":
                res |= {"is_table": True, "rows": [["기간", "12개월"], ["적용이자율", "2.60%"]], "cells": [], "n_rows": 2, "n_cols": 2}
            out[r["region_id"]] = res | {"trace": {"calls": ["vlm-9001"], "kinds": ["table_cells" if job["kind"] == "table" else "general"]}}
        return out


def test_missed_text_becomes_table_and_text_regions_and_ocr_moves(tmp_path):
    rec = record(tmp_path)
    reads = copy.deepcopy(READS)
    fake = FakeReader({})
    summary = S.run([rec], reads, fake, None, tmp_path / "trace")
    added = [r for r in rec["regions"] if r["region_id"].startswith("p-v0-S")]
    assert [(r["source"], r["table_candidate"]) for r in added] == [(S.SOURCE_GAP, True), (S.SOURCE_GAP, False)]
    assert sorted(kind for kind, _ in fake.jobs) == ["general", "table"]
    table = added[0]
    assert table["assigned_ocr_ids"] == ["p-v0-T001", "p-v0-T002", "p-v0-T003", "p-v0-T004"] and reads[table["region_id"]]["is_table"]
    left = {u["ocr_id"] for u in rec["unassigned_texts"]}
    assert left == {"p-v0-T006", "p-v0-T007"}                       # 이미 읽힌 부분 겹침 줄·한 글자 기호는 그대로
    assert len(rec["assignments"]) + len(rec["unassigned_texts"]) == rec["ocr_items_total"]
    assert rec["partition_checks"]["assigned"] == 6 and rec["partition_checks"]["unassigned"] == 2
    assert summary["added"] == 2 and load(tmp_path / "trace" / S.PLAN_FILE)["pages"][0]["missed_ocr"] == [
        "p-v0-T001", "p-v0-T002", "p-v0-T003", "p-v0-T004", "p-v0-T005"]


def test_duplicate_text_and_failed_read_are_dropped_and_reapply_matches(tmp_path):
    rec = record(tmp_path)
    original = copy.deepcopy(rec)
    reads = copy.deepcopy(READS)
    # 제목 줄을 이웃 영역(L001, 줄 높이만큼 넓힌 범위 안) 문구로 읽었다고 하면 중복으로 버린다
    S.run([rec], reads, FakeReader({"농협은행이 777명에게 쓴다": "이 예금은 예금자보호법에 따라"}), None, tmp_path / "trace")
    plan = load(tmp_path / "trace" / S.PLAN_FILE)["pages"][0]["candidates"]
    assert [(c["decision"], c["why"]) for c in plan] == [("add", None), ("drop", "text_in_region:p-v0-L001")]
    assert "p-v0-T005" in {u["ocr_id"] for u in rec["unassigned_texts"]}        # 버린 후보의 OCR 은 미배정으로 남음
    again = copy.deepcopy(original)
    assert S.reapply([again], tmp_path / "trace") == 1
    assert [r["region_id"] for r in again["regions"]] == [r["region_id"] for r in rec["regions"]]
    assert again["unassigned_texts"] == rec["unassigned_texts"] and again["assignments"] == rec["assignments"]
    assert S.reapply([again], tmp_path / "trace") == 0                             # 이미 더한 기록에는 다시 더하지 않음


def test_candidate_mostly_inside_existing_region_is_dropped_before_reading(tmp_path):
    rec = record(tmp_path)
    rec["unassigned_texts"] = [U(1, "미배정 문구 한 줄", [100, 120, 400, 220])]       # 덧댄 상자의 76% 가 L000(0~200) 안
    rec["ocr_items_total"] = 2
    fake = FakeReader({})
    S.run([rec], copy.deepcopy(READS), fake, None, tmp_path / "trace")
    c = load(tmp_path / "trace" / S.PLAN_FILE)["pages"][0]["candidates"][0]
    assert (c["decision"], c["why"]) == ("drop", "overlaps_existing_region") and fake.jobs == []


def test_grid_and_grouping_rules():
    a = [U(1, "기간", [0, 0, 40, 20]), U(2, "12개월", [150, 0, 200, 20]), U(3, "적용이자율", [0, 22, 60, 42]), U(4, "2.60%", [150, 22, 200, 42])]
    assert S.is_grid(a) and len(S.clusters(a)) == 1
    far = [U(1, "왼쪽", [0, 0, 40, 20]), U(2, "오른쪽", [900, 0, 960, 20])]          # 같은 줄이라도 6×줄 높이보다 멀면 다른 묶음
    assert len(S.clusters(far)) == 2 and not S.is_grid(far)
    assert S.contains("농협은행", "nh농협은행본점") and not S.contains("캡처", "캡션 처리")


def test_short_text_is_duplicate_only_when_boxes_overlap(tmp_path):
    """짧은 낱말은 다른 위치의 같은 낱말과 구별할 수 없어 후보 상자와 실제로 겹치는 영역의 문구만 본다(tt03 '기본금리' 항목명)."""
    rec = record(tmp_path)
    texts = {"p-v0-L001": "이예금은예금자보호법에따라보호됩니다농협은행"}
    near = {"region_id": "p-v0-S009", "bbox": [500, 560, 600, 590]}            # L001(600~) 과 겹치지 않음 → 남김
    assert S._decide(rec, near, {"text": "농협은행", "contract_ok": True}, texts) == (True, None)
    over = {"region_id": "p-v0-S009", "bbox": [500, 590, 600, 620]}            # L001 과 겹침 → 중복
    assert S._decide(rec, over, {"text": "농협은행", "contract_ok": True}, texts) == (False, "text_in_region:p-v0-L001")


def test_table_read_failure_retries_then_falls_back_to_text_region(tmp_path):
    """표 판독이 구조 검사에 실패하면 한 번 더 표로 읽고, 또 실패하면 일반 판독 문구로 글자 영역을 더한다(검토 표시)."""
    rec = record(tmp_path)
    reads = copy.deepcopy(READS)
    fake = FakeReader({}, table_fails=True)
    S.run([rec], reads, fake, None, tmp_path / "trace")
    kinds = [k for k, ids in fake.jobs if ids == ["p-v0-S000"]]
    assert kinds == ["table", "table", "general"]
    region = next(r for r in rec["regions"] if r["region_id"] == "p-v0-S000")
    assert not region["table_candidate"] and region["supplement_note"] == "supplement_table_structure_failed"
    assert reads["p-v0-S000"]["text"] == "기간 12개월 적용이자율 2.60%" and "is_table" not in reads["p-v0-S000"]
