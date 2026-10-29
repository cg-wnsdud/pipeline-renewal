"""오프라인 회귀 테스트. OCR 서버 대신 가짜 응답을 쓴다."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import vlm_contract  # noqa: E402
from layout_pipeline.assignment import OVERLAP_RULE, AssignmentRule, PartitionError, assign, verify_partition  # noqa: E402
from layout_pipeline.jsonio import sha256_file  # noqa: E402
from layout_pipeline.ocr_client import OcrClient  # noqa: E402
from layout_pipeline.pages import Page  # noqa: E402
from layout_pipeline.regions import OcrItem, Region, parse_view  # noqa: E402
from layout_pipeline.routing import (BoundaryPlanError, RoutingConfigError, decide, load_policy,  # noqa: E402
                                     validate_plan)
from layout_pipeline.stage1 import build_page  # noqa: E402

POLICY = load_policy(ROOT / "config" / "routing.json")
RULE = AssignmentRule()


def make_page(tmp_path: Path, w: int, h: int, pid: str = "x01_p01") -> Page:
    path = tmp_path / f"{pid}.png"
    Image.new("RGB", (w, h), "white").save(path)
    return Page(pid, "s", "x01", 1, w, h, str(path), sha256_file(path), {"kind": "image"})


def pruned(w, h, boxes, texts):
    return {"width": w, "height": h, "model_settings": {},
            "layout_det_res": {"boxes": [{"cls_id": 1, "label": lab, "score": 0.9, "coordinate": c} for lab, c in boxes]},
            "overall_ocr_res": {"rec_texts": [t for t, _ in texts], "rec_boxes": [b for _, b in texts],
                                "rec_scores": [0.99] * len(texts),
                                "rec_polys": [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]] for _, b in texts],
                                "text_det_params": {"limit_side_len": 736, "limit_type": "min", "max_side_limit": 4000}},
            "parsing_res_list": [{"block_id": 0, "block_label": "text", "block_bbox": [0, 0, 10, 10]}]}


# --- 경로 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("w,h,shape", [(1654, 2339, "a4"), (2339, 1654, "a4"), (1680, 1837, "square"),
                                       (1000, 1000, "square"), (2881, 5120, "general")])
def test_a4_square_general_route_whole_single_view(tmp_path, w, h, shape):
    page = make_page(tmp_path, w, h)
    d = decide(page, POLICY, {})
    assert (d.route, d.shape, d.status) == ("whole", shape, "READY")
    assert [v.crop for v in d.views] == [(0, 0, w, h)]
    assert d.to_dict()["vlm_pixel_budget_used"] is False


def test_long_without_plan_is_pending_not_split(tmp_path):
    page = make_page(tmp_path, 667, 6130)
    d = decide(page, POLICY, {})
    assert (d.route, d.status, d.views) == ("long", "BOUNDARY_PENDING", [])


def make_plan(page: Page, cuts, status="validated", **over) -> dict:
    internal = cuts[1:-1] if cuts else []
    plan = {"contract": "boundary-plan/2", "plan_version": "test-1", "page_id": page.page_id,
            "page_image_sha256": page.image_sha256, "page_size": [page.width, page.height],
            "coordinate_system": "page_px_xyxy", "axis": "y", "status": status, "cuts": cuts,
            "policy": {"name": "test-policy", "version": "0", "max_tile_extent_px": 2200, "status": "experimental"},
            "boundaries": [{"position": y, "evidence": {"protected_boxes_clear": True},
                            "semantic_check": {"method": "human_review", "verified_by": "tester", "result": "safe"}}
                           for y in internal]}
    return plan | over


def test_unvalidated_plan_is_not_executed(tmp_path):
    page = make_page(tmp_path, 667, 6130)
    plan = make_plan(page, [0, 1668, 3336, 4903, 6130], status="candidate")
    d = decide(page, POLICY, {page.page_id: plan})
    assert d.status == "BOUNDARY_CANDIDATE_UNVALIDATED" and d.views == []


def test_validated_plan_tiles_cover_page_without_overlap(tmp_path):
    page = make_page(tmp_path, 667, 6130)
    d = decide(page, POLICY, {page.page_id: make_plan(page, [0, 2000, 4100, 6130])})
    assert d.status == "READY"
    assert [v.crop for v in d.views] == [(0, 0, 667, 2000), (0, 2000, 667, 4100), (0, 4100, 667, 6130)]


@pytest.mark.parametrize("cuts", [[0, 2000, 6000], [100, 2000, 6130], [0, 3000, 2000, 6130], [0, 2000.5, 6130],
                                  [0, 3000, 6130]])   # 마지막은 타일 길이 3130 > 정책 한도 2200
def test_bad_cuts_rejected(tmp_path, cuts):
    page = make_page(tmp_path, 667, 6130)
    with pytest.raises(BoundaryPlanError):
        validate_plan(make_plan(page, cuts), page)


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("page_image_sha256"),                                        # 해시 누락
    lambda p: p.pop("plan_version"),
    lambda p: p.update(coordinate_system="tile_px"),
    lambda p: p.update(policy={"name": "x", "version": "0"}),                   # 한도 없음
    lambda p: p["boundaries"].pop(),                                            # 경계 근거 누락
    lambda p: p["boundaries"][0].update(evidence={}),
    lambda p: p["boundaries"][0].update(semantic_check={"method": "vlm_candidate_choice", "verified_by": "vlm",
                                                        "result": "safe"}),     # VLM 판정은 validated 불가
    lambda p: p["boundaries"][0]["semantic_check"].update(result="unsafe"),
])
def test_validated_plan_requires_full_evidence(tmp_path, mutate):
    page = make_page(tmp_path, 667, 6130)
    plan = make_plan(page, [0, 2000, 4100, 6130])
    mutate(plan)
    d = decide(page, POLICY, {page.page_id: plan})
    assert d.status == "BOUNDARY_PLAN_INVALID" and d.views == []


def test_plan_for_other_image_is_not_applied(tmp_path):
    """같은 page_id 라도 원본 해시가 다른 경계 문서는 쓰지 않고 경계 없음(자동 경계 대상)으로 둔다."""
    page = make_page(tmp_path, 667, 6130)
    plan = make_plan(page, [0, 2000, 4100, 6130]) | {"page_image_sha256": "0" * 64}
    d = decide(page, POLICY, {page.page_id: plan})
    assert d.status == "BOUNDARY_PENDING" and d.views == [] and "해시 불일치" in d.basis


def test_policy_cannot_split_a4(tmp_path):
    bad = json.loads((ROOT / "config" / "routing.json").read_text(encoding="utf-8")) | {"long_aspect_min": 1.4}
    path = tmp_path / "r.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(RoutingConfigError):
        load_policy(path)


# --- 영역 출처 ----------------------------------------------------------------------------------------

def test_regions_come_from_layout_det_only_with_offset(tmp_path):
    page = make_page(tmp_path, 667, 6130)
    plan = make_plan(page, [0, 3000, 6130], policy={"name": "t", "version": "0", "max_tile_extent_px": 3200})
    view = decide(page, POLICY, {page.page_id: plan}).views[1]
    p = pruned(667, 3130, [("table", [10.5, 20.25, 300.0, 400.0]), ("image", [0, 500, 600, 900])],
               [("가나다", [12, 30, 100, 50]), ("라마", [5, 1000, 50, 1020])])
    regions, items, blocks = parse_view(p, view)
    assert [r.source for r in regions] == ["prunedResult.layout_det_res.boxes"] * 2
    assert regions[0].bbox == [10.5, 3020.25, 300.0, 3400.0]           # offset 만 적용
    assert regions[0].raw_coordinate == [10.5, 20.25, 300.0, 400.0]
    assert regions[0].table_candidate and not regions[1].table_candidate
    assert regions[1].raw_label == "image"                              # image 라벨도 버리지 않음
    assert len(regions) == 2 and len(items) == 2                        # rec_boxes 가 Region 이 되지 않음
    assert items[0].ocr_bbox == [12, 3030, 100, 3050]
    assert blocks[0]["source"] == "prunedResult.parsing_res_list"


# --- 배정 -------------------------------------------------------------------------------------------

def _region(i, box):
    return Region(f"v-L{i:03d}", "p", "v", i, box, box, False, "text", 1, 0.9)


def _item(i, box, text="글"):
    return OcrItem(f"v-T{i:03d}", "p", "v", i, text, box, [], 0.9)


def test_assignment_statuses_and_partition():
    regions = [_region(0, [0, 0, 100, 100]), _region(1, [50, 0, 200, 100]), _region(2, [0, 200, 100, 300])]
    items = [
        _item(0, [210, 210, 260, 230]),            # 밖
        _item(1, [10, 210, 60, 230]),              # region 2 정확 포함
        _item(2, [60, 280, 140, 300]),             # region 2 를 크게 벗어남 → partial
        _item(3, [60, 10, 90, 30]),                # region 0, 1 모두 포함 → 모호
        _item(4, [10, 198, 60, 222]),              # 위로 2px 돌출: 엄격 기본은 partial, 0.25 완화는 배정
        _item(5, [5, 5, 5, 20]),                   # 면적 0
        _item(6, [101, 210, 140, 222]),            # 원래 bbox 가 영역과 1px 도 겹치지 않음 → outside
    ]
    out = assign(items, regions, RULE)
    status = {a["ocr_id"]: "assigned" for a in out["assigned"]} | {u["ocr_id"]: u["reason"] for u in out["unassigned_texts"]}
    assert RULE.strict
    assert status == {"v-T000": "outside_layout", "v-T001": "assigned", "v-T002": "partial_overlap",
                      "v-T003": "ambiguous_multiple_regions", "v-T004": "partial_overlap", "v-T005": "invalid_bbox",
                      "v-T006": "outside_layout"}
    relaxed = assign(items, regions, AssignmentRule(core_shrink=0.25))
    tol = next(a for a in relaxed["assigned"] if a["ocr_id"] == "v-T004")
    assert tol["exact_inside"] is False and tol["protrusion_px"] == 2
    assert out["checks"]["passed"] and relaxed["checks"]["passed"]
    assert all(u["review_required"] and not u["final"] and u["source"] == "prunedResult.overall_ocr_res"
               for u in out["unassigned_texts"])
    assert [r.bbox for r in regions] == [[0, 0, 100, 100], [50, 0, 200, 100], [0, 200, 100, 300]]  # bbox 불변


def test_center_inside_is_not_enough():
    regions = [_region(0, [0, 0, 100, 100])]
    for rule in (RULE, AssignmentRule(core_shrink=0.25)):
        out = assign([_item(0, [40, 60, 160, 80])], regions, rule)   # 중심 (100,70) 은 경계 위지만 절반이 밖
        assert out["unassigned_texts"][0]["reason"] == "partial_overlap"


def test_margin_only_is_not_reported_as_outside():
    regions = [_region(0, [0, 0, 100, 100])]
    out = assign([_item(0, [96, 40, 140, 60])], regions, AssignmentRule(core_shrink=0.25))  # 여백 4px 만 걸침
    assert out["unassigned_texts"][0]["reason"] == "margin_overlap_only"
    out = assign([_item(0, [96, 40, 140, 60])], regions, RULE)
    assert out["unassigned_texts"][0]["reason"] == "partial_overlap"


def test_overlap_best_assigns_each_detected_line_once_and_preserves_only_outside():
    # 중첩 영역에서는 최대 IoA, 동률이면 작은 영역을 선택한다. 아주 작은 교집합도 기록하되 검토한다.
    regions = [_region(0, [0, 0, 200, 100]), _region(1, [0, 0, 100, 100]),
               _region(2, [100, 0, 200, 100])]
    items = [_item(0, [10, 10, 50, 30]), _item(1, [80, 10, 160, 30]),
             _item(2, [195, 10, 245, 30]), _item(3, [220, 50, 260, 70])]
    out = assign(items, regions, OVERLAP_RULE)
    by_id = {a["ocr_id"]: a for a in out["assigned"]}
    assert [by_id[f"v-T{i:03d}"]["region_id"] for i in range(3)] == ["v-L001", "v-L000", "v-L002"]
    assert by_id["v-T002"]["inside_ratio"] == 0.1
    assert "low_ocr_overlap" in by_id["v-T002"]["assignment_review"]
    assert [u["ocr_id"] for u in out["unassigned_texts"]] == ["v-T003"]
    assert out["unassigned_texts"][0]["reason"] == "outside_layout"
    assert out["checks"]["passed"]


def test_overlap_best_does_not_cross_tile_views():
    regions = [_region(0, [0, 0, 100, 100])]
    item = OcrItem("other-T000", "p", "other", 0, "다른 타일", [10, 10, 50, 30], [], 0.9)
    out = assign([item], regions, OVERLAP_RULE)
    assert not out["assigned"]
    assert out["unassigned_texts"][0]["reason"] == "outside_layout"


def test_weak_overlap_is_assigned_but_not_used_as_certain_vlm_ocr():
    from layout_pipeline import page_read, vlm_tasks

    regions = [_region(0, [0, 0, 100, 100]), _region(1, [50, 0, 150, 100])]
    assignment = assign([_item(0, [95, 10, 295, 30], "경계 문구")], regions, OVERLAP_RULE)
    assigned = assignment["assigned"][0]
    region = {"region_id": assigned["region_id"], "bbox": regions[1].bbox, "table_candidate": False}
    record = {"assignments": assignment["assigned"], "unassigned_texts": assignment["unassigned_texts"]}
    assert vlm_tasks.region_context(record, region)["ocr_draft"] == []
    assert vlm_tasks.candidate_context(record, region, "crop/3")["ocr_candidates"] == []
    why = page_read.verify_reasons(record, region, {"crop": [0, 0, 300, 100]},
                                   {"contract_ok": True, "text": "", "uncertain": False})
    assert "low_ocr_overlap_assigned" in why
    assert "multiple_region_overlap_assigned" in why


def test_partition_detects_duplicates_and_losses():
    items = [_item(0, [0, 0, 1, 1]), _item(1, [0, 0, 1, 1])]
    with pytest.raises(PartitionError):
        verify_partition(items, [], [{"ocr_id": "v-T000", "region_id": "x"}], [])
    with pytest.raises(PartitionError):
        verify_partition(items, [], [], [{"ocr_id": "v-T000"}, {"ocr_id": "v-T000"}, {"ocr_id": "v-T001"}])


# --- OCR 호출 기록과 페이지 빌드 ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_document_frame_table_is_read_as_text(tmp_path):
    """table-container-as-text/1(docs/19): OCR 글자가 많은 표 상자(본문 틀)는 표 후보에서 빼고 글 영역으로 읽는다. 작은 표는 그대로."""
    page = make_page(tmp_path, 1654, 2339)
    d = decide(page, POLICY, {})
    long_lines = [(f"대출대상 안내 문장 {i:02d} 계약금 5% 이상 납부한 분양계약자 중 소득 증빙 가능자", [120, 120 + i * 25, 880, 140 + i * 25])
                  for i in range(30)]
    p = pruned(1654, 2339, [("table", [100, 100, 900, 900]), ("table", [100, 1000, 900, 1100])],
               long_lines + [("기간 12개월", [120, 1010, 400, 1040])])

    def post(url, json, timeout):
        return FakeResponse({"logId": "x", "errorCode": 0, "result": {"layoutParsingResults": [{"prunedResult": p}]}})
    client = OcrClient("http://fake", 5, {"useTableRecognition": False}, tmp_path / "raw", tmp_path / "views", post=post)
    rec, pr = client.call(page, d.views[0])
    from dataclasses import asdict
    record = build_page(page, d, [asdict(rec)], {d.views[0].view_id: pr}, RULE)
    big, small = record["regions"]
    assert big["table_candidate"] is False and big["table_container"]["ocr_lines"] == 30 and big["raw_label"] == "table"
    assert small["table_candidate"] is True and "table_container" not in small
    assert any(i["code"] == "TABLE_CONTAINER_READ_AS_TEXT" for i in record["issues"])


def test_hwp_front_layer_covering_page_is_detected():
    """docs/19: 쪽 대부분을 덮는 '글 앞으로' 레이어만 본문을 가리는 것으로 본다(글 뒤로·작은 그림은 제외)."""
    from layout_pipeline.hwp_bridge import covering_layers
    surface = {"page_css_size": [793.71, 1122.51], "layer_items": [
        {"kind": "floating_image", "page_no": 1, "wrap": "IN_FRONT_OF_TEXT", "box_pt": [0.0, 0.0, 619.06, 857.15]},
        {"kind": "floating_image", "page_no": 1, "wrap": "BEHIND_TEXT", "box_pt": [0.0, 0.0, 595.0, 842.0]},
        {"kind": "floating_image", "page_no": 2, "wrap": "IN_FRONT_OF_TEXT", "box_pt": [10.0, 10.0, 200.0, 100.0]}]}
    hit = covering_layers({"review_surface": surface})
    assert [(h["page_no"], h["wrap"]) for h in hit] == [(1, "IN_FRONT_OF_TEXT")] and hit[0]["cover"] == 1.0
    assert covering_layers({"review_surface": {"layer_items": surface["layer_items"]}}) == []


def test_whole_page_single_call_and_vlm_plan(tmp_path):
    page = make_page(tmp_path, 1654, 2339)
    d = decide(page, POLICY, {})
    sent = []

    def post(url, json, timeout):
        sent.append(json)
        p = pruned(1654, 2339, [("table", [100, 100, 900, 500]), ("text", [100, 600, 900, 700])],
                   [("항목 금리", [120, 120, 400, 160]), ("본문", [120, 610, 500, 650]), ("메뉴", [1200, 50, 1300, 80])])
        return FakeResponse({"logId": "x", "errorCode": 0, "result": {"layoutParsingResults": [{"prunedResult": p}]}})

    client = OcrClient("http://fake", 5, {"useTableRecognition": False}, tmp_path / "raw", tmp_path / "views", post=post)
    rec, p = client.call(page, d.views[0])
    assert len(sent) == 1 and rec.ok and rec.sent_sha256 == page.image_sha256 and rec.input_crop == [0, 0, 1654, 2339]
    assert "layoutThreshold" not in sent[0]
    from dataclasses import asdict
    record = build_page(page, d, [asdict(rec)], {d.views[0].view_id: p}, RULE)
    assert record["partition_checks"]["passed"]
    assert [u["ocr_text"] for u in record["unassigned_texts"]] == ["메뉴"]
    tasks = {q["region_id"]: q["task"] for q in record["vlm_plan"]["requests"]}
    assert tasks == {f"{page.page_id}-v0-L000": "table_check", f"{page.page_id}-v0-L001": "read_text"}
    assert all(q["image_scope"] != "line_sheet" for q in record["vlm_plan"]["requests"])


# --- VLM 계약 ---------------------------------------------------------------------------------------

def _regions_dicts():
    return [{"region_id": "a", "page_id": "p", "view_id": "v", "bbox": [0, 0, 10, 10], "table_candidate": True},
            {"region_id": "b", "page_id": "p", "view_id": "v", "bbox": [0, 20, 10, 30], "table_candidate": False}]


def test_table_check_only_for_candidates():
    regions = _regions_dicts()
    bad = [{"task": "table_check", "region_id": "a", "bbox": [0, 0, 10, 10], "image_scope": "region_crop"},
           {"task": "table_check", "region_id": "b", "bbox": [0, 20, 10, 30], "image_scope": "region_crop"}]
    with pytest.raises(vlm_contract.ContractError):
        vlm_contract.validate_requests(bad, regions)
    sheet = [{"task": "table_check", "region_id": "a", "bbox": [0, 0, 10, 10], "image_scope": "region_crop"},
             {"task": "read_text", "region_id": "b", "bbox": [0, 20, 10, 30], "image_scope": "line_sheet"}]
    with pytest.raises(vlm_contract.ContractError):
        vlm_contract.validate_requests(sheet, regions)


def test_review_not_decided_by_uncertain_only(tmp_path):
    from layout_pipeline import vlm_tasks
    record = {"assignments": [{"region_id": "a", "ocr_text": "금리 연 2.45%", "ocr_id": "t1"}],
              "unassigned_texts": [], "diagnostics": {"region_overlaps": []}}
    region = {"region_id": "a", "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
    rv = vlm_tasks.review_reasons(record, region, {"text": "금리 연 2.54%", "uncertain": False, "contract_errors": []})
    assert rv["needs_review"] and "digit_mismatch" in rv["quality_reasons"]
    rv = vlm_tasks.review_reasons(record, region, {"text": None, "uncertain": False, "contract_errors": []})
    assert "read_failed" in rv["quality_reasons"]


def test_vlm_validate_rejects_missing_duplicate_and_string_bool():
    from types import SimpleNamespace
    from layout_pipeline import vlm_tasks
    req = {"mode": "page", "region_ids": ["a", "b"]}
    call = SimpleNamespace(error=None, parsed={"results": [{"region_id": "a", "text": "x", "uncertain": "false"},
                                                            {"region_id": "a", "text": "y", "uncertain": False}]})
    out = vlm_tasks.validate(call, req)
    assert not out["a"]["contract_ok"] and not out["b"]["contract_ok"]
    assert any("uncertain 타입" in e for e in out["a"]["contract_errors"])
    tcall = SimpleNamespace(error=None, parsed={"region_id": "t", "is_table": "true", "text": "x", "rows": [],
                                                 "uncertain": False})
    t = vlm_tasks.validate(tcall, {"mode": "table", "region_ids": ["t"]})
    assert not t["t"]["contract_ok"] and t["t"]["is_table"] is None


def test_review_flags_special_token_and_incomplete_rows():
    from layout_pipeline import vlm_tasks
    record = {"assignments": [], "unassigned_texts": [], "diagnostics": {"region_overlaps": []}}
    region = {"region_id": "t", "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
    leaked = {"text": '기간\n12개월"} <|file_separator|> {', "uncertain": False, "contract_errors": [],
              "is_table": True, "rows": [["기간", "12개월"]]}
    assert "special_token_in_text" in vlm_tasks.review_reasons(record, region, leaked)["quality_reasons"]
    partial = {"text": "가입대상\n개인\n납입한도\n연간 2천만원\n의무가입기간\n3년", "uncertain": False, "contract_errors": [],
               "is_table": True, "rows": [["가입대상", "개인"]]}
    assert "table_rows_incomplete" in vlm_tasks.review_reasons(record, region, partial)["quality_reasons"]


# --- 긴 이미지 경계 실험 ----------------------------------------------------------------------------

def test_scout_windows_cover_page_with_overlap():
    from layout_pipeline import boundary
    wins = boundary.scout_windows(667, 6130)
    assert wins[0][1] == 0 and wins[-1][3] == 6130
    assert all(b[1] < a[3] for a, b in zip(wins, wins[1:]))          # 겹침(탐색 문맥용)
    assert all(w[3] - w[1] <= round(667 * boundary.SCOUT_ASPECT) for w in wins)


def test_boundary_candidates_avoid_protected_boxes():
    from layout_pipeline import boundary
    boxes = [{"kind": "ocr", "bbox": [0, 100, 600, 130], "text": "a"},
             {"kind": "ocr", "bbox": [0, 140, 600, 170], "text": "b"},      # 줄 간격 10
             {"kind": "layout", "bbox": [0, 300, 600, 400], "text": None}]
    cands, gap = boundary.candidates(boxes, 500)
    ys = [c["y"] for c in cands]
    assert all(not (96 <= y <= 174) and not (296 <= y <= 404) for y in ys)
    big = [c for c in cands if 174 < c["y"] < 296]
    assert big and big[0]["gap_px"] >= 100 and gap == 10


# --- 통합 실행(판독 단계·최종 계약) -----------------------------------------------------------------

class FakeVlm:
    """요청 내용에 따라 정해진 응답을 돌려주는 가짜 VLM. call_id 는 실제 클라이언트처럼 순번."""
    def __init__(self, behavior):
        self.behavior, self.count = behavior, 0

    def chat(self, text, images, schema, name, max_tokens, meta=None):
        from layout_pipeline.vlm_client import VlmCall
        self.count += 1
        ids = meta["region_ids"]
        content, finish, err = self.behavior(meta["kind"], ids)
        parsed = None
        if content is not None:
            try:
                parsed = json.loads(content)
            except ValueError as exc:
                err = (err or "") + f"JSON 파싱 실패: {exc}"
        return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, finish, {}, content, parsed, err, [], meta or {})


def _record(tmp_path):
    page = make_page(tmp_path, 1654, 2339)
    d = decide(page, POLICY, {})
    p = pruned(1654, 2339, [("table", [100, 100, 900, 500]), ("text", [100, 600, 900, 700]), ("text", [100, 800, 900, 900]),
                            ("image", [1000, 100, 1500, 400])],
               [("구분 금리", [120, 120, 400, 160]), ("본문", [120, 610, 500, 650]), ("둘째", [120, 810, 500, 850])])
    from dataclasses import asdict
    client = OcrClient("http://fake", 5, {}, tmp_path / "raw", tmp_path / "views",
                       post=lambda url, json, timeout: FakeResponse({"logId": "x", "errorCode": 0,
                                                                     "result": {"layoutParsingResults": [{"prunedResult": p}]}}))
    rec, pr = client.call(page, d.views[0])
    return build_page(page, d, [asdict(rec)], {d.views[0].view_id: pr}, RULE)


def test_reader_isolates_batch_failure_and_keeps_failed_regions(tmp_path):
    from layout_pipeline.final_output import build_result
    from layout_pipeline.read_stage import Reader
    record = _record(tmp_path)

    def behavior(kind, ids):
        if kind == "table":       # 표 요청은 출력 잘림
            return '{"region_id": "x", "is_table": tr', "length", "finish_reason=length"
        if len(ids) > 1:          # 묶음 요청은 통째로 실패
            return None, None, "HTTP 500"
        rid = ids[0]
        text = "" if rid.endswith("L003") else "본문"      # 그림 영역은 글자 없음
        return json.dumps({"results": [{"region_id": rid, "text": text, "uncertain": False}]}), "stop", None

    reader = Reader(FakeVlm(behavior), tmp_path / "trace", concurrency=1)
    reads = reader.read_pages([record], max_images=8, margin_rule="none")
    result, regions = build_result([record], reads, {})
    by = {r["region_id"].rsplit("-", 1)[-1]: r for r in result["regions"]}
    assert result["contract_checks"]["passed"]
    assert by["L000"]["text"] is None and by["L000"]["text_status"] == "read_failed"
    assert by["L000"]["table"] == {"status": "unknown", "rows": None}            # false 로 바꾸지 않음
    assert by["L000"]["ocr_reference"]["final"] is False and by["L000"]["ocr_reference"]["assigned"] == ["구분 금리"]
    assert by["L001"]["text"] == "본문" and by["L003"]["text_status"] == "no_text"
    assert len(by["L001"]["trace_calls"]) == 2                                   # 묶음 실패 → 단독 1회
    codes = [i["code"] for i in result["issues"]]
    assert "TABLE_CHECK_UNKNOWN" in codes and "REGION_READ_FAILED" in codes
    assert len(result["regions"]) == 4                                            # 실패 영역도 남음


def test_ocr_reference_keeps_original_order_and_line_level_agreement():
    """f12_p02 L019 형: 배정 2줄 뒤에 경계 OCR 을 붙이면 순서가 바뀌어 잘못된 불일치 경고가 났다."""
    from layout_pipeline import vlm_tasks
    record = {"assignments": [{"region_id": "r", "ocr_id": "v-T041", "ocr_text": "※고향사랑금리는 가입고객 모두에게 적용되며"},
                              {"region_id": "r", "ocr_id": "v-T042", "ocr_text": "금리시장 상황에 따라 변동될 수 있습니다."}],
              "unassigned_texts": [{"ocr_id": "v-T040", "ocr_text": "고향사랑금리 0.05%p(2026.08.21.기준)", "reason": "partial_overlap",
                                    "candidates": [{"region_id": "r", "inside_ratio": 0.99}]}],
              "diagnostics": {"region_overlaps": []}}
    region = {"region_id": "r", "crossing_ocr_ids": ["v-T040"], "ambiguous_ocr_ids": []}
    ctx = vlm_tasks.region_context(record, region)
    assert [c["ocr_id"] for c in ctx["ocr_candidates"]] == ["v-T040", "v-T041", "v-T042"]      # 원래 OCR 순서
    assert [c["source"] for c in ctx["ocr_candidates"]] == ["boundary_unassigned", "assigned", "assigned"]
    vlm = "고향사랑금리 0.05%p(2026.08.21. 기준)\n※ 고향사랑금리는 가입고객 모두에게 적용되며\n금리시장 상황에 따라 변동될 수 있습니다."
    rv = vlm_tasks.review_reasons(record, region, {"text": vlm, "uncertain": False, "contract_errors": []})
    assert "ocr_vlm_mismatch" not in rv["quality_reasons"]
    assert "ocr_crosses_region_boundary" in rv["context_reasons"]                           # 맥락 근거는 유지
    swapped = "금리시장 상황에 따라 변동될 수 있습니다.\n고향사랑금리 0.05%p(2026.08.21. 기준)\n※ 고향사랑금리는 가입고객 모두에게 적용되며"
    rv2 = vlm_tasks.review_reasons(record, region, {"text": swapped, "uncertain": False, "contract_errors": []})
    assert "ocr_vlm_mismatch" not in rv2["quality_reasons"]                                  # 줄 순서 재구성은 오류 아님
    wrong = "고향사랑금리 0.50%p(2026.08.21. 기준)\n금리시장 상황"                              # 숫자 변경·줄 누락은 경고
    rv3 = vlm_tasks.review_reasons(record, region, {"text": wrong, "uncertain": False, "contract_errors": []})
    assert {"ocr_vlm_mismatch", "digit_mismatch"} <= set(rv3["quality_reasons"])


def test_crop2_changes_only_instruction_and_ocr_reference_form():
    """판독 원칙 v2: 이미지·묶음·스키마는 crop/1 과 같고, OCR 참고는 순서 없는 후보 목록(정렬)과 출처만 준다."""
    from layout_pipeline import vlm_tasks
    record = {"assignments": [{"region_id": "r", "ocr_id": "v-T002", "ocr_text": "452,100원의적세효과가 있습니다"},
                              {"region_id": "r", "ocr_id": "v-T001", "ocr_text": "절세효과"}],
              "unassigned_texts": [{"ocr_id": "v-T000", "ocr_text": "NH농협금융", "reason": "partial_overlap",
                                    "candidates": [{"region_id": "r", "inside_ratio": 0.99}]}]}
    region = {"region_id": "r", "bbox": [10, 10, 90, 50], "table_candidate": False}
    img = Image.new("RGB", (100, 60), "white")
    v1 = vlm_tasks.crop_request(record, [region], img, 0, "crop/1")
    v2 = vlm_tasks.crop_request(record, [region], img, 0, "crop/2")
    assert v2["version"] == "crop/2" and v2["schema"] == v1["schema"] and v2["region_ids"] == v1["region_ids"]
    assert [i.size for i in v2["images"]] == [i.size for i in v1["images"]]
    item = v2["items"][0]
    assert "ocr_draft" not in item and "boundary_ocr" not in item
    assert item["ocr_candidates"] == sorted(item["ocr_candidates"], key=lambda c: (c["text"], c["source"]))
    assert {c["source"] for c in item["ocr_candidates"]} == {"in_region", "boundary"}
    assert "OCR 후보 목록의 순서는 읽기 순서가 아닙니다" in v2["text"] and "원본의 오타·특이 표현 보존" in v2["text"]
    with pytest.raises(ValueError):
        vlm_tasks.crop_request(record, [region], img, 4, "crop/2")
    t3 = vlm_tasks.table_request(record, region, img)
    t4 = vlm_tasks.table_request(record, region, img, "table-check/4")
    assert t4["schema"] == t3["schema"] and "ocr_candidates" in t4["items"][0] and "ocr_draft" in t3["items"][0]
def test_overlap_duplicate_text_is_context_review_without_changing_text():
    """f12_p02 L019/L025 형: 겹친 두 영역이 같은 위치 문구를 함께 읽음 → 맥락 검토 근거만 추가, 문구·영역 유지."""
    from layout_pipeline.final_output import mark_overlap_duplicates
    def reg(rid, text):
        return {"region_id": rid, "text": text, "needs_review": False, "review": {"quality": [], "context": [], "measures": {}}}
    a = reg("L019", "고향사랑금리 0.05%p(2026.08.21. 기준)\n※ 고향사랑금리는 가입고객 모두에게 적용되며")
    b = reg("L025", "특별금리 고향사랑금리 0.05%p(2026.08.21. 기준)")
    c = reg("L030", "특별금리 고향사랑금리 0.05%p(2026.08.21. 기준)")    # 겹치지 않는 영역의 실제 반복
    mark_overlap_duplicates([a, b, c], [{"a": "L019", "b": "L025"}])
    assert "overlap_duplicate_text" in a["review"]["context"] and "overlap_duplicate_text" in b["review"]["context"]
    assert a["needs_review"] and b["needs_review"] and not c["review"]["context"]
    assert a["text"].startswith("고향사랑금리") and b["text"].startswith("특별금리")


def test_agreement_accepts_label_value_pairing_but_flags_changed_value():
    """v2 판독: 떨어진 OCR 줄(항목/값)을 한 줄로 짝지어도 불일치가 아니다. 값이 바뀌면 여전히 경고."""
    from layout_pipeline import vlm_tasks
    ocr = ["가입대상", "가입금액", "개인(적립식)", "월 1만원 이상 ~ 50만원 이내"]
    paired = "가입대상 개인(적립식)\n가입금액 월 1만원 이상 ~ 50만원 이내"
    a, b = vlm_tasks.ocr_vlm_agreement(ocr, paired)
    assert a == 1.0 and b == 1.0
    changed = "가입대상 법인\n가입금액 월 5만원 이상 ~ 90만원 이내"
    a2, b2 = vlm_tasks.ocr_vlm_agreement(ocr, changed)
    assert min(a2, b2) < vlm_tasks.MISMATCH_MIN_COVER


def test_crop3_drops_barely_touching_boundary_candidates():
    """v3: 영역 안 비율 0.5 미만 경계 OCR 은 후보로 주지 않고(f01 L008 형), 0.5 이상은 비율과 함께 준다(f12_p02 L019 형)."""
    from layout_pipeline import vlm_tasks
    record = {"assignments": [{"region_id": "r", "ocr_id": "v-T002", "ocr_text": "BANK"}],
              "unassigned_texts": [{"ocr_id": "v-T000", "ocr_text": "NH더퍼스트기업대출", "reason": "partial_overlap",
                                    "candidates": [{"region_id": "r", "inside_ratio": 0.0021}]},
                                   {"ocr_id": "v-T001", "ocr_text": "고향사랑금리 0.05%p", "reason": "partial_overlap",
                                    "candidates": [{"region_id": "r", "inside_ratio": 0.99}]}]}
    region = {"region_id": "r", "bbox": [10, 10, 90, 50], "table_candidate": False}
    img = Image.new("RGB", (100, 60), "white")
    v2 = vlm_tasks.crop_request(record, [region], img, 0, "crop/2")["items"][0]["ocr_candidates"]
    v3 = vlm_tasks.crop_request(record, [region], img, 0, "crop/3")
    c3 = v3["items"][0]["ocr_candidates"]
    assert {c["text"] for c in v2} == {"BANK", "NH더퍼스트기업대출", "고향사랑금리 0.05%p"}
    assert {c["text"] for c in c3} == {"BANK", "고향사랑금리 0.05%p"}
    assert next(c for c in c3 if c["source"] == "boundary")["inside_ratio"] == 0.99
    assert "후보 목록을 옮겨 적거나 나열하지 마세요" in v3["text"]
    t5 = vlm_tasks.table_request(record, region, img, "table-check/5")
    assert "text 를 빈 문자열로 두지 마세요" in t5["text"] and t5["schema"] == vlm_tasks.table_request(record, region, img)["schema"]


def test_table_rows_fill_empty_text_and_artifacts_are_flagged():
    """f02 L001 형: rows 는 맞는데 text='' → rows 를 text 로 옮기고 맥락 근거로 표시. 응답 잔여물은 품질 근거."""
    from layout_pipeline.read_stage import rows_text_fallback
    from layout_pipeline import vlm_tasks
    region = {"table_candidate": True}
    res = {"text": "", "is_table": True, "rows": [["", "일임형 ISA", "신탁형 ISA"], ["운용재량", "전문가", "투자자"]]}
    out = rows_text_fallback(region, res)
    assert out["text"] == "일임형 ISA 신탁형 ISA\n운용재량 전문가 투자자" and out["text_from_rows"]
    assert rows_text_fallback(region, res | {"is_table": False})["text"] == ""          # 표 아님이면 그대로
    assert rows_text_fallback({"table_candidate": False}, res)["text"] == ""
    assert vlm_tasks.OUTPUT_ARTIFACT_RE.search('0.21%"}<be_thoughtful>{') and not vlm_tasks.OUTPUT_ARTIFACT_RE.search('1인당 "1억원까지" 보호')


def test_page_first_reader_reads_page_then_verifies_only_flagged_regions(tmp_path):
    """페이지 우선 경로: 페이지 1회로 모든 ID 를 채우고, OCR 누락 근거가 있는 영역만 crop 확인한다. 표 필드는 유지."""
    from layout_pipeline.read_stage import Reader, reconstruct_reads, read_path
    from layout_pipeline.vlm_client import VlmCall
    img_path = tmp_path / "p.png"
    Image.new("RGB", (400, 300), "white").save(img_path)
    view = {"view_id": "p-v0", "page_id": "p", "kind": "whole", "crop": [0, 0, 400, 300], "width": 400, "height": 300, "aspect": 1.33}
    regions = [{"region_id": "p-v0-L000", "page_id": "p", "view_id": "p-v0", "bbox": [10, 10, 200, 60], "table_candidate": False,
                "crossing_ocr_ids": [], "ambiguous_ocr_ids": []},
               {"region_id": "p-v0-L001", "page_id": "p", "view_id": "p-v0", "bbox": [10, 100, 390, 200], "table_candidate": True,
                "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}]
    record = {"page": {"page_id": "p", "image_path": str(img_path)}, "status": "READY", "route": {"views": [view]},
              "regions": regions,
              "assignments": [{"region_id": "p-v0-L000", "ocr_id": "p-v0-T000", "ocr_text": "중도해지 및", "ocr_bbox": [10, 10, 100, 40]},
                              {"region_id": "p-v0-L000", "ocr_id": "p-v0-T001", "ocr_text": "만기후 금리", "ocr_bbox": [10, 30, 100, 60]},
                              {"region_id": "p-v0-L001", "ocr_id": "p-v0-T002", "ocr_text": "기간 12개월", "ocr_bbox": [10, 100, 300, 140]}],
              "unassigned_texts": [], "diagnostics": {"region_overlaps": []}}

    class Fake:
        count = 0

        def chat(self, text, images, schema, name, max_tokens, meta):
            self.count += 1
            if meta["kind"] == "page":
                assert len(images) == 2 and images[0].size == (400, 300)        # 원본 + 표시 사본
                parsed = {"regions": [{"region_id": "p-v0-L000", "text": "중도해지 및", "uncertain": False},      # 둘째 줄 누락
                                      {"region_id": "p-v0-L001", "text": "기간 12개월", "uncertain": False}],
                          "tables": [{"region_id": "p-v0-L001", "is_table": True, "rows": [["기간", "12개월"]]}]}
            else:
                # (가) 1차 판독이 있는 영역은 다시 쓰지 않고 보완(patch)만 받는다.
                assert meta["kind"] == "patch" and meta["region_ids"] == ["p-v0-L000"]
                assert '"page_text": "중도해지 및"' in text
                parsed = {"results": [{"region_id": "p-v0-L000", "missing": [{"text": "만기후 금리", "after": "중도해지 및"}],
                                       "corrections": [], "remove": [], "uncertain": False}]}
            return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)

    reader = Reader(Fake(), tmp_path / "trace", 1)
    reader.verify_mode = "patch"                                       # 이전 방식(region-patch/1) 재현
    out = reader.read_pages_page_first([record], max_images=8)
    assert out["p-v0-L000"]["text"] == "중도해지 및\n만기후 금리"
    assert out["p-v0-L000"]["trace"]["page_text"] == "중도해지 및"
    assert "ocr_text_missing_in_page_read" in out["p-v0-L000"]["trace"]["verify_reasons"]
    assert out["p-v0-L001"]["rows"] == [["기간", "12개월"]] and read_path(out["p-v0-L001"]["trace"]) == "page"
    assert read_path(out["p-v0-L000"]["trace"]) == "page+patch"
    again = reconstruct_reads(tmp_path / "trace", [record])            # 저장된 호출만으로 같은 결과
    assert again["p-v0-L000"]["text"] == out["p-v0-L000"]["text"] and again["p-v0-L001"]["rows"] == out["p-v0-L001"]["rows"]


def test_page_response_id_mismatch_fails_only_affected_regions():
    """페이지 응답이 일부 ID 를 빠뜨리거나 중복하면 그 영역만 실패로 보고 나머지는 유지한다."""
    from types import SimpleNamespace
    from layout_pipeline import page_read
    parsed = {"regions": [{"region_id": "a", "text": "가", "uncertain": False},
                          {"region_id": "b", "text": "나", "uncertain": False},
                          {"region_id": "b", "text": "나2", "uncertain": False}]}
    out = page_read.validate_page(SimpleNamespace(error=None, parsed=parsed), {"region_ids": ["a", "b", "c"], "table_ids": []})
    assert out["a"]["contract_ok"] and out["a"]["text"] == "가"
    assert not out["b"]["contract_ok"] and not out["c"]["contract_ok"]


def test_auto_boundary_rejects_forbidden_relation_and_recomposes(tmp_path):
    """section-pack/2: 고른 경계가 제목–본문이면 그 위치를 빼고 다시 구성한다. 정책 검증 전에는 candidate 만 만든다."""
    from types import SimpleNamespace
    from layout_pipeline import auto_boundary
    from layout_pipeline.vlm_client import VlmCall
    w, h = 400, 2400
    page = SimpleNamespace(page_id="x", width=w, height=h, image_sha256="s",
                           open_image=lambda: Image.new("RGB", (w, h), "white"))
    # 상자: 빈 띠가 y≈600, 900, 1300, 1800 에 생기도록 배치
    spans = [(10, 580), (620, 880), (920, 1280), (1320, 1780), (1820, 2390)]
    boxes = [{"kind": "ocr", "bbox": [10, a, 390, b], "text": f"t{a}"} for a, b in spans]
    policy = {"name": "section-pack", "version": "2", "status": "approved", "validation": {"status": "pending"},
              "allowed_relations": ["independent_sections", "table_and_footnote"], "rejected_relations": ["heading_and_body", "unknown"],
              "target_aspect": 2.5, "scope": {"width_px": [0, 9999], "min_aspect": 3.0}}

    class Fake:
        n = 0

        def chat(self, text, images, schema, name, max_tokens, meta):
            self.n += 1
            rel = "heading_and_body" if meta["y"] == 900 else "independent_sections"
            parsed = {"above_id": schema["properties"]["above_id"]["enum"][0], "below_id": schema["properties"]["below_id"]["enum"][0],
                      "relation": rel, "reason": "t"}
            return VlmCall(f"bnd-{self.n:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)

    plan = auto_boundary.make_plan(page, boxes, Fake(), tmp_path, policy, [])
    assert 900 not in plan["cuts"] and plan["cuts"][0] == 0 and plan["cuts"][-1] == h
    assert all(b - a <= w * 2.5 for a, b in zip(plan["cuts"], plan["cuts"][1:]))
    assert plan["status"] == "candidate"                                  # 정책 검증(pending) 전
    assert "900" in plan["rejected_positions"]
    assert all(b["semantic_check"]["result"] == "safe" for b in plan["boundaries"])


def test_auto_boundary_judges_one_round_concurrently_with_same_plan(tmp_path):
    """한 라운드의 판정 대상은 동시에 보내도(상한 concurrency) 직렬 판정과 같은 계획이 나온다."""
    import threading
    import time as _time
    from types import SimpleNamespace
    from layout_pipeline import auto_boundary
    from layout_pipeline.vlm_client import VlmCall
    w, h = 400, 4000
    page = SimpleNamespace(page_id="x", width=w, height=h, image_sha256="s",
                           open_image=lambda: Image.new("RGB", (w, h), "white"))
    spans = [(10, 580), (620, 880), (920, 1280), (1320, 1780), (1820, 2390), (2430, 2980), (3020, 3480), (3520, 3990)]
    boxes = [{"kind": "ocr", "bbox": [10, a, 390, b], "text": f"t{a}"} for a, b in spans]
    policy = {"name": "section-pack", "version": "2", "status": "approved", "validation": {"status": "pending"},
              "allowed_relations": ["independent_sections"], "rejected_relations": ["heading_and_body", "unknown"],
              "target_aspect": 2.5, "scope": {"width_px": [0, 9999], "min_aspect": 3.0}}

    class Fake:
        def __init__(self):
            self.n = self.active = self.peak = 0
            self.lock = threading.Lock()

        def chat(self, text, images, schema, name, max_tokens, meta):
            with self.lock:
                self.n += 1
                n = self.n
                self.active += 1
                self.peak = max(self.peak, self.active)
            _time.sleep(0.05)
            with self.lock:
                self.active -= 1
            rel = "heading_and_body" if meta["y"] == 900 else "independent_sections"
            parsed = {"above_id": schema["properties"]["above_id"]["enum"][0], "below_id": schema["properties"]["below_id"]["enum"][0],
                      "relation": rel, "reason": "t"}
            return VlmCall(f"bnd-{n:04d}", "t", 0.05, 200, "stop", {}, "", parsed, None, [], meta)

    serial, par = Fake(), Fake()
    p1 = auto_boundary.make_plan(page, boxes, serial, tmp_path / "s", policy, [])
    p3 = auto_boundary.make_plan(page, boxes, par, tmp_path / "p", policy, [], concurrency=3)
    assert serial.peak == 1 and 1 < par.peak <= 3 and serial.n == par.n
    assert p3["cuts"] == p1["cuts"] and p3["rounds"] == p1["rounds"]
    drop = lambda j: {k: v for k, v in j.items() if k != "call_id"}  # noqa: E731
    assert {y: drop(j) for y, j in p3["rejected_positions"].items()} == {y: drop(j) for y, j in p1["rejected_positions"].items()}
    assert [b["semantic_check"]["relation"] for b in p3["boundaries"]] == [b["semantic_check"]["relation"] for b in p1["boundaries"]]


def test_patch_keeps_page_structure_and_applies_only_unique_anchors():
    """(가) 보완 반영: 1차 판독의 순서는 그대로, 기준 문자열이 한 번만 있을 때만 적용. 중복은 교정으로 제거."""
    from layout_pipeline import page_read
    page = "중도해지 및 만기 후 금리 가입일 당시 고시된 중도해지 및\n만기 후 금리 만기 후 금리 준용"
    t, ok, bad = page_read.apply_patch(page, {"missing": [], "remove": [],
                                              "corrections": [{"wrong": "만기 후 금리 만기 후 금리 준용", "right": "만기 후 금리 준용"}]})
    assert t == "중도해지 및 만기 후 금리 가입일 당시 고시된 중도해지 및\n만기 후 금리 준용" and not bad
    t2, _, bad2 = page_read.apply_patch(page, {"missing": [], "remove": [], "corrections": [{"wrong": "만기 후 금리", "right": "X"}]})
    assert t2 == page and bad2                                   # 여러 번 있는 기준은 적용하지 않음
    t3, _, _ = page_read.apply_patch("세금 차이\n두 개의 금융상품", {"missing": [], "corrections": [], "remove": ["세금 차이"]})
    assert t3 == "두 개의 금융상품"
    assert page_read.repeated_text(["만기후 금리", "만기 후 금리 준용"], page)


def test_vlm_bands_cut_only_between_regions():
    """(나) 크게 줄어드는 입력만 띠로 나누고, 영역 bbox 를 가로지르지 않는다."""
    from layout_pipeline import page_read
    view = {"view_id": "v", "crop": [0, 0, 1800, 4000]}
    regs = [{"region_id": f"r{i}", "bbox": [10, a, 1790, b]} for i, (a, b) in enumerate([(0, 900), (950, 1900), (1950, 2900), (2950, 3990)])]
    plan = page_read.vlm_bands(view, regs)
    assert plan["split"] and all(b - a <= plan["max_band_px"] for a, b in plan["bands"])
    for a, b in plan["bands"]:
        for r in regs:
            assert r["bbox"][3] <= a or r["bbox"][1] >= b or (a <= r["bbox"][1] and r["bbox"][3] <= b)
    small = {"view_id": "s", "crop": [0, 0, 1000, 1500]}
    assert not page_read.vlm_bands(small, regs[:1])["split"]


def test_reread_selects_complete_candidate_and_keeps_first_on_conflict_or_failure(tmp_path):
    """region-reread/1(docs/10): 재판독은 대상 영역 하나의 완결 후보. 고정 정책으로 선택하고 1차·재판독을 모두 trace 에 남긴다."""
    from layout_pipeline.read_stage import Reader, reconstruct_reads
    from layout_pipeline.vlm_client import VlmCall
    img_path = tmp_path / "p.png"
    Image.new("RGB", (400, 300), "white").save(img_path)
    view = {"view_id": "p-v0", "page_id": "p", "kind": "whole", "crop": [0, 0, 400, 300], "width": 400, "height": 300, "aspect": 1.33}
    regions = [{"region_id": f"p-v0-L00{i}", "page_id": "p", "view_id": "p-v0", "bbox": [10, 10 + 60 * i, 390, 60 + 60 * i],
                "table_candidate": False, "crossing_ocr_ids": [], "ambiguous_ocr_ids": []} for i in range(3)]
    asg = [{"region_id": "p-v0-L000", "ocr_id": "p-v0-T000", "ocr_text": "중도해지 및", "ocr_bbox": [10, 10, 100, 30]},
           {"region_id": "p-v0-L000", "ocr_id": "p-v0-T001", "ocr_text": "만기후 금리", "ocr_bbox": [10, 32, 100, 55]},
           {"region_id": "p-v0-L001", "ocr_id": "p-v0-T002", "ocr_text": "가입대상 개인", "ocr_bbox": [10, 70, 200, 110]},
           {"region_id": "p-v0-L002", "ocr_id": "p-v0-T003", "ocr_text": "가입금액 월 1만원", "ocr_bbox": [10, 130, 200, 170]}]
    record = {"page": {"page_id": "p", "image_path": str(img_path)}, "status": "READY", "route": {"views": [view]},
              "regions": regions, "assignments": asg, "unassigned_texts": [], "diagnostics": {"region_overlaps": []}}

    class Fake:
        count = 0

        def chat(self, text, images, schema, name, max_tokens, meta):
            self.count += 1
            if meta["kind"] == "page":
                parsed = {"regions": [{"region_id": "p-v0-L000", "text": "중도해지 및", "uncertain": False},       # 누락 → 확인
                                      {"region_id": "p-v0-L001", "text": "가입대상", "uncertain": False},          # 누락 → 확인
                                      {"region_id": "p-v0-L002", "text": "가입금액", "uncertain": False}]}         # 누락 → 확인
                return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)
            assert meta["kind"] == "reread" and len(images) == 2 and meta["condition"] == "B"   # crop + 문맥
            rid = meta["region_ids"][0]
            if rid == "p-v0-L000":
                parsed = {"region_id": rid, "text": "중도해지 및 만기후 금리", "uncertain": False, "changes": ["missing_added"], "notes": ""}
                return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)
            if rid == "p-v0-L001":                                         # 계약 실패 → 1차 유지 + reread_failed
                return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, None, None, None, None, None, "ReadTimeout", [], meta)
            parsed = {"region_id": rid, "text": "", "uncertain": False, "changes": ["none"], "notes": ""}   # 손상 → 1차 유지
            return VlmCall(f"vlm-{self.count:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)

    out = Reader(Fake(), tmp_path / "trace", 1).read_pages_page_first([record], max_images=8)
    assert out["p-v0-L000"]["text"] == "중도해지 및 만기후 금리" and out["p-v0-L000"]["trace"]["reread_source"] == "reread"
    assert out["p-v0-L000"]["trace"]["first_text"] == "중도해지 및"
    assert out["p-v0-L001"]["text"] == "가입대상" and out["p-v0-L001"]["reread_review"] == ["reread_failed"]
    assert out["p-v0-L002"]["text"] == "가입금액" and out["p-v0-L002"]["reread_review"] == ["reread_conflict"]
    again = reconstruct_reads(tmp_path / "trace", [record])
    assert {k: v["text"] for k, v in again.items()} == {k: v["text"] for k, v in out.items()}


def test_reread2_changes_object_and_truncated_text_salvage():
    """region-reread/2(docs/20): changes 는 분류별 참/거짓 객체(→ 목록). 뒤 필드에서 잘린 응답은 완성된 text 만 꺼낸다.
    text 자체가 잘렸거나 /1 기록이면 꺼내지 않는다(기존 실행 재구성은 그대로)."""
    from types import SimpleNamespace
    from layout_pipeline import page_read
    rid = "p-v0-L000"
    req = {"region_ids": [rid], "version": page_read.REREAD_VERSION}
    ok = SimpleNamespace(error=None, finish_reason="stop", content="", parsed={
        "region_id": rid, "text": "가", "uncertain": False, "notes": "",
        "changes": {k: k == "order_or_block_fixed" for k in page_read.REREAD_CHANGE_KEYS}})
    assert page_read.validate_reread(ok, req)[rid]["changes"] == ["order_or_block_fixed"]
    cut = '{"region_id": "%s", "text": "매년 3월\\n※ 예금의 \\"이자\\"", "uncertain": false, "changes": ["missing_added", "missing_' % rid
    trunc = SimpleNamespace(error="finish_reason=length (출력 잘림 가능)", finish_reason="length", content=cut, parsed=None)
    out = page_read.validate_reread(trunc, req)[rid]
    assert out["contract_ok"] and out["salvaged"] and out["text"] == '매년 3월\n※ 예금의 "이자"'
    assert not page_read.validate_reread(trunc, {"region_ids": [rid], "version": "region-reread/1"})[rid]["contract_ok"]
    half = SimpleNamespace(error="x", finish_reason="length", content='{"region_id": "%s", "text": "매년 3월 반복 반복' % rid, parsed=None)
    assert not page_read.validate_reread(half, req)[rid]["contract_ok"]
    other = SimpleNamespace(error="x", finish_reason="length", content=cut.replace(rid, "p-v0-L009"), parsed=None)
    assert not page_read.validate_reread(other, req)[rid]["contract_ok"]


def test_reread_starts_without_waiting_for_other_page_reads(tmp_path):
    """판독 단위 하나의 1차 판독이 끝나면 그 단위의 재판독이 다른 단위의 1차 판독을 기다리지 않고 시작한다.
    병합 결과는 호출 번호 순서와 상관없이 같다(같은 영역의 재판독은 그 영역 1차 판독 뒤)."""
    import threading
    from layout_pipeline.read_stage import Reader, reconstruct_reads
    from layout_pipeline.vlm_client import VlmCall
    records = []
    for pid in ("a", "b"):
        img_path = tmp_path / f"{pid}.png"
        Image.new("RGB", (400, 300), "white").save(img_path)
        view = {"view_id": f"{pid}-v0", "page_id": pid, "kind": "whole", "crop": [0, 0, 400, 300], "width": 400, "height": 300,
                "aspect": 1.33}
        region = {"region_id": f"{pid}-v0-L000", "page_id": pid, "view_id": f"{pid}-v0", "bbox": [10, 10, 390, 60],
                  "table_candidate": False, "crossing_ocr_ids": [], "ambiguous_ocr_ids": []}
        asg = [{"region_id": region["region_id"], "ocr_id": f"{pid}-v0-T000", "ocr_text": "중도해지 및", "ocr_bbox": [10, 10, 100, 30]},
               {"region_id": region["region_id"], "ocr_id": f"{pid}-v0-T001", "ocr_text": "만기후 금리", "ocr_bbox": [10, 32, 100, 55]}]
        records.append({"page": {"page_id": pid, "image_path": str(img_path)}, "status": "READY", "route": {"views": [view]},
                        "regions": [region], "assignments": asg, "unassigned_texts": [], "diagnostics": {"region_overlaps": []}})
    reread_started = threading.Event()

    class Fake:
        def __init__(self):
            self.count = 0
            self.lock = threading.Lock()
            self.overlapped = None

        def chat(self, text, images, schema, name, max_tokens, meta):
            with self.lock:
                self.count += 1
                n = self.count
            rid = meta["region_ids"][0]
            if meta["kind"] == "page" and meta["page_id"] == "a":
                parsed = {"regions": [{"region_id": rid, "text": "중도해지 및", "uncertain": False}]}          # 누락 → 재판독
            elif meta["kind"] == "page":
                self.overlapped = reread_started.wait(timeout=5)      # a 의 재판독이 b 1차 판독 중에 시작되는지
                parsed = {"regions": [{"region_id": rid, "text": "중도해지 및\n만기후 금리", "uncertain": False}]}
            else:
                assert meta["kind"] == "reread" and rid == "a-v0-L000"
                reread_started.set()
                parsed = {"region_id": rid, "text": "중도해지 및 만기후 금리", "uncertain": False, "changes": ["missing_added"], "notes": ""}
            return VlmCall(f"vlm-{n:04d}", "t", 0.1, 200, "stop", {}, "", parsed, None, [], meta)

    fake = Fake()
    out = Reader(fake, tmp_path / "trace", 2).read_pages_page_first(records, max_images=8)
    assert fake.overlapped is True
    assert out["a-v0-L000"]["text"] == "중도해지 및 만기후 금리" and out["a-v0-L000"]["trace"]["reread_source"] == "reread"
    assert out["b-v0-L000"]["text"] == "중도해지 및\n만기후 금리"
    again = reconstruct_reads(tmp_path / "trace", records)
    assert {k: v["text"] for k, v in again.items()} == {k: v["text"] for k, v in out.items()}


def test_reread_that_brings_text_from_outside_the_box_is_not_selected():
    """reread-select/2(docs/16 §5.2): 상자 안 누락 보충은 채택, 1차 판독에 없던 상자 밖 OCR 줄(다른 영역 글자)을 가져온 후보는
    1차 판독 유지 + reread_outside_text. 문구를 고치지 않고 후보 중 하나를 고른다."""
    from layout_pipeline import page_read
    region = {"region_id": "p-v0-L000", "page_id": "p", "bbox": [10, 10, 390, 60], "table_candidate": False}
    asg = [{"region_id": "p-v0-L000", "ocr_id": "p-v0-T000", "ocr_text": "중도해지 및", "ocr_bbox": [10, 10, 100, 30]},
           {"region_id": "p-v0-L000", "ocr_id": "p-v0-T001", "ocr_text": "만기후 금리", "ocr_bbox": [10, 32, 100, 55]},
           {"region_id": "p-v0-L002", "ocr_id": "p-v0-T003", "ocr_text": "가입금액 월 1만원 이상", "ocr_bbox": [10, 130, 200, 170]}]
    record = {"regions": [region], "assignments": asg, "unassigned_texts": []}
    ok = {"contract_ok": True, "uncertain": False}
    inside = page_read.select_reread(record, region, "중도해지 및", ok | {"text": "중도해지 및\n만기후 금리"})
    assert inside["source"] == "reread" and inside["text"] == "중도해지 및\n만기후 금리"
    outside = page_read.select_reread(record, region, "중도해지 및", ok | {"text": "중도해지 및\n만기후 금리\n가입금액 월 1만원 이상"})
    assert outside["source"] == "first_pass" and outside["text"] == "중도해지 및" and outside["review"] == ["reread_outside_text"]
    assert outside["measures"]["reread_outside_lines"] == ["가입금액 월 1만원 이상"]
    kept = page_read.select_reread(record, region, "중도해지 및\n가입금액 월 1만원 이상", ok | {"text": "중도해지 및\n만기후 금리\n가입금액 월 1만원 이상"})
    assert kept["source"] == "reread"          # 1차에도 있던 상자 밖 줄은 새 유입이 아니다(최종 검토 표시는 text_from_outside_box 가 맡음)


def test_hwp_structure_regions_replace_duplicate_visual_regions(tmp_path):
    """hwp-structure/1(docs/20): HWP 페이지는 기존 parser-pipeline hybrid 경로대로 DOM 행을 구조 Region(문구 = HWP 원문, VLM 판독 없음)
    으로 쓰고, 구조 Region 이 이미 담은 화면 검출 영역은 버리고 그 밖(그림 속 글자)만 시각 Region 으로 남긴다."""
    from layout_pipeline import hwp_structure, page_read
    from layout_pipeline.read_stage import merge_entries
    rows = [{"node_id": f"r{i}", "page_no": 1, "bbox": [10, 10 + 40 * i, 390, 40 + 40 * i],
             "cells": [{"text": f"항목{i}", "bbox": [10, 10 + 40 * i, 100, 40 + 40 * i], "node_id": f"c{i}a"},
                       {"text": f"값 {i}원", "bbox": [100, 10 + 40 * i, 390, 40 + 40 * i], "node_id": f"c{i}b"}]} for i in range(3)]
    (tmp_path / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    page = Page("h_p01", "s", "h", 1, 400, 300, str(tmp_path / "p.png"), "x",
                {"kind": "hwp", "page_pt": [300, 225], "conversion": {"dom_rows": {"path": str(tmp_path / "rows.json"),
                                                                                    "page_css_size": [400, 300]}}})
    dup = Region("h_p01-v0-L000", "h_p01", "h_p01-v0", 0, [12.0, 52.0, 388.0, 78.0], [12.0, 52.0, 388.0, 78.0], False, "text", 1, 0.9)
    logo = Region("h_p01-v0-L001", "h_p01", "h_p01-v0", 1, [10.0, 200.0, 120.0, 260.0], [10.0, 200.0, 120.0, 260.0], False, "image", 2, 0.9)
    items = [OcrItem("h_p01-v0-T000", "h_p01", "h_p01-v0", 0, "항목1 값 1원", [14, 55, 380, 75], [], 0.9),
             OcrItem("h_p01-v0-T001", "h_p01", "h_p01-v0", 1, "NH농협은행", [20, 210, 110, 250], [], 0.9)]
    out = hwp_structure.apply(page, "h_p01-v0", [dup, logo], items)
    ids = [r.region_id for r in out["regions"]]
    assert ids == ["h_p01-h0-H000", "h_p01-h0-H001", "h_p01-h0-H002", "h_p01-v0-L001"]
    assert out["summary"]["visual_suppressed"] == 1 and out["summary"]["structured_regions"] == 3
    region = out["regions"][1].to_dict() | {"text_source": hwp_structure.VERSION, "hwp": out["meta"]["h_p01-h0-H001"]}
    rec = {"regions": [region, logo.to_dict()], "page": {"page_id": "h_p01"}}
    assert page_read.view_regions(rec, {"view_id": "h_p01-v0"}) == [logo.to_dict()]          # 구조 Region 은 VLM 판독 대상 아님
    got = merge_entries([], {r["region_id"]: (rec, r) for r in rec["regions"]})["h_p01-h0-H001"]
    assert got["text"] == "항목1\n값 1원" and got["contract_ok"] and got["trace"]["kinds"] == ["hwp_structure"]
    assert got["is_table"] and [c["text"] for c in got["cells"]] == ["항목1", "값 1원"] and got["trace"]["table_read"] == "hwp-dom-row/1"
    plain = Page("x_p01", "s", "x", 1, 400, 300, "x.png", "x", {"kind": "pdf"})
    assert hwp_structure.apply(plain, "x_p01-v0", [dup], items) is None
