"""docs/14 의미 단계(분류·영역 배정·라벨링·layout-result/3) 오프라인 테스트. 모델 호출 없음(가짜 클라이언트).

합성 입력은 계약·분기 검사용이다. 실제 모델의 분류·라벨 품질 평가가 아니다.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import classify, contract_v3, labeling, semantic  # noqa: E402
from layout_pipeline import semantic_assets as A  # noqa: E402
from layout_pipeline.file_result import build_file_results, write_file_results  # noqa: E402
from layout_pipeline.final_output import build_result  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.report_html import build_report  # noqa: E402
from layout_pipeline.vlm_client import VlmCall, png_part  # noqa: E402


# ---- 합성 파싱 snapshot(layout-result/2) ----

def _record(tmp, sub, fid, page_no, name, kind, regions, unassigned=(), status="READY"):
    pid = f"{fid}_p{page_no:02d}"
    img = tmp / "stage1" / "pages" / f"{pid}.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), "white").save(img)
    view = {"view_id": f"{pid}-v0", "page_id": pid, "kind": "whole", "crop": [0, 0, 400, 300], "width": 400, "height": 300, "aspect": 0.75}
    regs = [{"region_id": f"{pid}-v0-L{i:03d}", "page_id": pid, "view_id": f"{pid}-v0", "bbox": [10, 10 + 40 * i, 390, 40 + 40 * i],
             "table_candidate": r.get("table", False), "source": "test", "source_index": i, "raw_label": "text",
             "crossing_ocr_ids": [], "ambiguous_ocr_ids": []} for i, r in enumerate(regions)]
    un = [{"ocr_id": f"{pid}-v0-T{i:03d}", "page_id": pid, "ocr_text": t, "ocr_bbox": [300, 280, 390, 295], "score": 0.9,
           "reason": "outside_layout", "candidates": [], "empty_text": False, "source": "test"} for i, t in enumerate(unassigned)]
    rec = {"page": {"page_id": pid, "submission_id": sub, "file_id": fid, "page_no": page_no, "width": 400, "height": 300,
                    "image_path": str(img), "image_sha256": "a" * 64,
                    "source": {"kind": kind, "source_name": name, "source_sha256": "1" * 64, "page_count": 1}},
           "route": {"route": "whole", "views": [view], "basis": "", "boundary_plan": None}, "status": status,
           "regions": regs, "assignments": [], "unassigned_texts": un, "ocr_items_total": len(un),
           "diagnostics": {"region_overlaps": []}, "issues": []}
    reads = {}
    for g, r in zip(regs, regions):
        res = {"text": r["text"], "uncertain": False, "contract_ok": r["text"] is not None, "contract_errors": [],
               "trace": {"calls": ["vlm-0001"], "kinds": ["page"]}}
        if r.get("table"):
            rows = r["rows"]
            res |= {"is_table": True, "rows": rows, "n_rows": len(rows), "n_cols": len(rows[0]),
                    "cells": [{"row": i, "col": j, "rowspan": 1, "colspan": 1, "text": c} for i, row in enumerate(rows) for j, c in enumerate(row)],
                    "trace": {"calls": ["vlm-0001", "vlm-0002"], "kinds": ["page", "table_cells"], "table_read": "table-cells/1"}}
        reads[g["region_id"]] = res
    return rec, reads


def make_parse(tmp: Path, files: list[dict]) -> Path:
    """files: [{sub, fid, name, kind, pages: [[region...], ...], unassigned: [[text...] per page], status}] → <tmp>/parse"""
    out = tmp / "parse"
    recs, reads = [], {}
    for f in files:
        for n, regions in enumerate(f["pages"], 1):
            un = (f.get("unassigned") or [[]] * len(f["pages"]))[n - 1]
            rec, rd = _record(out, f["sub"], f["fid"], n, f["name"], f["kind"], regions, un, f.get("status", "READY"))
            rec["page"]["source"]["page_count"] = len(f["pages"])
            recs.append(rec)
            reads |= rd
    s1 = out / "stage1"
    dump(s1 / "run.json", {"routing_policy": {"long_aspect_min": 3.0, "status": "provisional"}, "pdf_dpi": 200,
                           "assignment_rule": {"name": "core-containment/1", "core_shrink": 0.0}})
    dump(s1 / "calls.json", [])
    for r in recs:
        dump(s1 / "records" / f"{r['page']['page_id']}.json", r)
    settings = {"stage1": str(s1), "read_mode": "page-first", "page_request": "page/2", "verify_request": "region-reread/1",
                "table_read": "table-cells/1", "table_request": "table-check/3", "temperature": 0,
                "assignment_rule": {"name": "core-containment/1", "core_shrink": 0.0}}
    v1, _ = build_result(recs, reads, settings)
    (out / "trace" / "vlm").mkdir(parents=True)
    docs = build_file_results(ROOT, out, v1, recs, s1, out / "trace")
    index = write_file_results(out, docs)
    dump(out / "files" / "index.json", {"contract": "layout-result/2/index", "files": index})
    return out


def R(text, table=False, rows=None):
    return {"text": text, "table": table, "rows": rows}


DEPOSIT = [{"sub": "s1", "fid": "a01", "name": "13. 예금성상품-적립식 광고.pdf", "kind": "pdf", "pages": [
    [R("NH농협은행"), R("NH튼튼적금"), R("가입기간 12개월 월 10만원 이상"),
     R("", ), R("금"), R("구분 금리", True, [["구분", "금리"], ["기본금리", "연 3.0%"]])],
    [R("※ 예금자보호법에 따라 1인당 1억원까지 보호됩니다."), R(None)]]}]


def _enum(schema):
    """요청 스키마의 영역 별칭(응답 regions 객체의 필수 키, 순서 그대로)."""
    return schema["properties"]["regions"]["required"]


class Fake:
    """kind 별 응답 함수(meta, schema, text) → parsed dict | None(HTTP 실패)."""

    def __init__(self, **fns):
        self.fns, self.count, self.keys, self._lock = fns, 0, [], threading.Lock()
        self.active = self.peak = 0
        self.delay = 0.0

    def chat(self, text, images, schema, name, max_tokens, meta=None):
        with self._lock:
            self.count += 1
            n = self.count
            self.keys.append(meta["key"])
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        parsed = self.fns[meta["kind"]](meta, schema, text)
        with self._lock:
            self.active -= 1
        err = None if parsed is not None else "HTTP 500: test"
        return VlmCall(f"sem-{n:04d}", "t", 0.01, 200 if parsed is not None else 500, "stop" if parsed is not None else None,
                       {"prompt_tokens": 1, "completion_tokens": 1}, json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
                       parsed, err, [png_part(i)[1] for i in images],
                       (meta or {}) | {"prompt": text, "schema": schema, "max_tokens": max_tokens, "temperature": 0})


def observe_one(meta, schema, text):
    ids = _enum(schema)
    regions = []
    for a in ids:
        scope, t = ("shared", ["t1"]) if a in ("P1-R01", "P2-R01") else ("unknown", []) if a == "P1-R05" else ("specific", ["t1"])
        regions.append((a, {"scope": scope, "targets": t}))
    return {"targets": [{"tid": "t1", "name": "NH튼튼적금", "group": "예금성", "templates": ["deposit_saving"],
                         "evidence": [{"feature": "deposit_saving.name", "region": "P1-R02", "quote": "NH튼튼적금"},
                                      {"feature": "deposit_saving.monthly", "region": "P1-R03", "quote": "월 10만원 이상"}]}],
            "mentions": [], "regions": dict(regions), "parts": []}


def label_scripted(meta, schema, text):
    ids = _enum(schema)
    table = {"P1-R01": [{"label": "회사명", "quote": "NH농협은행"}],
             "P1-R02": [{"label": "상품명", "quote": "NH튼튼적금"}],
             "P1-R03": [{"label": "가입기간", "quote": "가입기간 12개월"}, {"label": "가입금액", "quote": "월 10만원 이상"}],
             "P1-R06": [{"label": "금리", "quote": "연 3.0%"}],                                   # 표 셀에만 있는 인용
             "P2-R01": [{"label": "예금자보호", "quote": "예금자보호법에 따라 1인당 2억원"}]}       # 원문에 없는 인용 → 버림
    return {"regions": {a: table.get(a, []) for a in ids}}


def EMPTY_LABELS(meta, schema, text):
    return {"regions": {a: [] for a in _enum(schema)}}


def run_fake(tmp, files, fake, concurrency=2) -> tuple[dict, Path]:
    parse = make_parse(tmp, files)
    out = tmp / "sem"
    rep = semantic.run(parse, out, fake, concurrency, classification_mode="legacy")
    return rep, out


# ---- 자산 ----

def test_assets_match_manifest_catalog_and_scope():
    assert A.verify_assets() == []
    # 원본 템플릿 카탈로그(실행 중에는 읽지 않는 출처 자료)와 라벨 스키마·분류 정책의 템플릿이 맞는지 확인한다.
    cat = json.loads((A.ASSET_DIR / "ad_templates.json").read_text(encoding="utf-8"))["templates"]
    for t in A.templates():
        s = A.label_schema(t["label_schema"])
        assert t["template_id"] in cat and s["scope"] == t["template_id"]
        assert s["labels"] == [i["gubun"] for i in cat[t["template_id"]]["items"]]
        assert cat[t["template_id"]]["product_group"] == t["product_group"]
    assert A.label_schema("investment-isa-general")["labels"] == ["회사명", "수수료", "유의사항", "심의번호"]
    assert A.component_templates() == {"FUND": "투자성상품-펀드", "ETF": "투자성상품-ETF", "ELB": "투자성상품-ELB"}
    assert not any("etf" in k or "elb" in k for k in A.LABEL_SCHEMA_KEYS)       # 운용상품 전용 스키마를 만들지 않음
    assert len(set(A.feature_ids())) == len(A.feature_ids())
    assert all(A.feature_template(f) in A.template_codes() + ["other"] for f in A.feature_ids())


def test_knowledge_has_no_source_ad_and_keeps_full_examples():
    for k in A.LABEL_SCHEMA_KEYS:
        s = A.label_schema(k)
        labels, text = A.knowledge([k])
        assert labels == s["labels"]
        for row in s["positives"] + s["negatives"]:
            assert row["source_ad"] not in text
            assert " ".join(row["quote"].split()) in text                       # 예시 문구를 자르지 않음
    labels, text = A.knowledge(["loan-broker-balance", "loan-named"])           # 템플릿 둘 = 허용 라벨 합집합
    assert set(labels) == set(A.label_schema("loan-broker-balance")["labels"]) | set(A.label_schema("loan-named")["labels"])


# ---- 인용 검사 ----

def test_quote_where_is_strict_and_finds_table_cells():
    region = {"text": "가입기간 12개월\n월 10만원 이상", "table": {"rows": [["구분", "금리"], ["기본", "연 3.0%"]],
                                                                   "cells": [{"row": 1, "col": 1, "text": "연 3.0%"}]}}
    assert classify.quote_where("가입기간 12개월 월 10만원", region)["text"]             # 줄바꿈·공백 차이만
    assert classify.quote_where("연 3.0%", region)["cells"] == [[1, 1]]
    assert classify.quote_where("가입기간 24개월", region) is None                        # 일부만 겹침 → 근거 아님
    assert classify.quote_where("가입기간 … 이상", region) is None                        # 중간 생략 인용 불인정
    assert classify.quote_where("", region) is None


# ---- 분류 요청의 중립성 ----

@pytest.mark.parametrize("name,fid,sub", [("neutral.jpeg", "x01", "sub-x"), ("1. 대출성상품-상품명 노출 광고1.pdf", "loan99", "g99")])
def test_classification_request_does_not_depend_on_file_name_or_ids(tmp_path, name, fid, sub):
    base = make_parse(tmp_path / "a", DEPOSIT)
    other = copy.deepcopy(DEPOSIT)
    other[0] |= {"name": name, "fid": fid, "sub": sub}
    alt = make_parse(tmp_path / "b", other)
    reqs = []
    for parse in (base, alt):
        docs, paths = semantic.load_parse(parse)
        b = classify.build_bundle(docs, paths)
        reqs.append(classify.observe_request(b, classify.plan_units(b)[0], True))
    assert reqs[0]["text"] == reqs[1]["text"] and reqs[0]["schema"] == reqs[1]["schema"]
    assert [png_part(i)[1]["sha256"] for i in reqs[0]["images"]] == [png_part(i)[1]["sha256"] for i in reqs[1]["images"]]
    for leak in (name, fid, sub, "적립식 광고", "a01", "s1_"):
        assert leak not in reqs[1]["text"]
    srcs = {r["source_ad"] for k in A.LABEL_SCHEMA_KEYS for r in A.label_schema(k)["positives"] + A.label_schema(k)["negatives"]}
    assert not any(s in reqs[0]["text"] for s in srcs)                          # 예시 출처 광고 이름이 새지 않음


# ---- 전체 흐름(단일 관측) ----

def test_single_unit_flow_statuses_labels_and_contract(tmp_path):
    fake = Fake(observe=observe_one, label=label_scripted)
    rep, out = run_fake(tmp_path, DEPOSIT, fake)
    assert rep["checks"]["passed"], json.dumps(load(out / "files" / "contract-check.json"), ensure_ascii=False)
    d = load(out / "files" / "a01" / "result.json")
    assert d["contract"] == "layout-result/3" and d["parse"]["contract"] == "layout-result/2"
    p = d["products"]
    assert [(x["product_id"], x["name"], x["status"], x["product_group"]) for x in p] == [("product_001", "NH튼튼적금", "classified", "예금성")]
    assert p[0]["templates"] == [{"template_id": "예금성상품-적립식", "role": "product", "label_schema": "deposit-saving"}]
    reg = {r["region_id"]: r for r in d["regions"]}
    g = lambda i, n=1: reg[f"a01_p{n:02d}-v0-L{i:03d}"]  # noqa: E731
    assert g(0)["product_assignment"] == {"status": "completed", "scope": "shared", "product_ids": ["product_001"], "basis": "model"}
    assert g(0)["labeling"]["items"] == [{"product_id": "product_001", "label_ids": ["회사명"]}]
    assert g(2)["labeling"]["items"][0]["label_ids"] == ["가입기간", "가입금액"]                   # 한 영역 여러 라벨
    assert g(3)["text_status"] == "no_text" and g(3)["labeling"] == {"status": "completed", "items": [{"product_id": "product_001", "label_ids": []}]}
    assert g(4)["product_assignment"] == {"status": "partial", "scope": "unknown", "product_ids": [], "basis": "model"}  # 미확정
    assert g(4)["labeling"] == {"status": "not_run", "items": []} and "assignment_unknown" in g(4)["semantic_review"]["codes"]
    assert g(5)["labeling"]["items"][0]["label_ids"] == ["금리"]                                   # 표 셀 인용
    assert g(0, 2)["labeling"]["items"][0]["label_ids"] == []                                       # 인용 불일치 라벨은 버림
    assert "label_quote_rejected" in g(0, 2)["semantic_review"]["codes"] and g(0, 2)["semantic_review"]["required"]
    assert g(1, 2)["text_status"] == "read_failed" and g(1, 2)["labeling"]["status"] == "not_run"
    assert "no_final_text" in g(1, 2)["semantic_review"]["codes"]
    assert g(1)["labeling"]["items"][0]["label_ids"] == ["상품명"] and not g(1)["semantic_review"]["required"]
    assert all(r["review"] == load(tmp_path / "parse" / "files" / "a01" / "result.json")["regions"][i]["review"]
               for i, r in enumerate(d["regions"]))                                                   # 파싱 검토는 그대로
    trace = load(out / "trace" / "semantic" / "s1.json")
    assert trace["labels"]["a01_p02-v0-L000"]["rejected"]["product_001"][0]["why"] == "quote_not_in_region"
    assert trace["labels"]["a01_p01-v0-L005"]["evidence"]["product_001"][0]["where"]["cells"] == [[1, 1]]
    final = json.dumps(d, ensure_ascii=False)
    for absent in ('"quote"', '"prompt"', '"feature"', '"analysis"', '"reason"', '"evidence"'):
        assert absent not in final                                                                   # 근거·프롬프트는 trace 에만
    assert (out / "files" / "index.html").read_text(encoding="utf-8").count("product_001") >= 1


def test_label_request_has_only_product_schema_and_full_text(tmp_path):
    long_text = "※ " + "가" * 3000 + " 끝문장"
    files = copy.deepcopy(DEPOSIT)
    files[0]["pages"][0][2] = R(long_text)
    seen = []

    def label(meta, schema, text):
        seen.append((schema, text))
        return label_scripted(meta, schema, text)
    run_fake(tmp_path, files, Fake(observe=observe_one, label=label))
    allowed = set(A.label_schema("deposit-saving")["labels"])
    for schema, text in seen:
        enum = next(iter(schema["properties"]["regions"]["properties"].values()))["items"]["properties"]["label"]["enum"]
        assert set(enum) == allowed
        assert "수수료" not in enum and "심의번호" in enum
    assert any(json.dumps(long_text, ensure_ascii=False)[1:-1] in t for _, t in seen)                 # 본문 생략 없음
    assert all("NH튼튼적금" in t for _, t in seen)


# ---- 페이지별 관측 + 통합 ----

def _paged(monkeypatch):
    monkeypatch.setattr(classify, "SINGLE_UNIT_LIMITS", {"pages": 1, "regions": 80, "chars": 16000, "images": 6})


TWO_FILES = [{"sub": "s2", "fid": "b01", "name": "x.png", "kind": "image", "pages": [[R("NH올원e통장"), R("입출금이 자유로운 통장")]]},
             {"sub": "s2", "fid": "b02", "name": "y.png", "kind": "image", "pages": [[R("우대금리 연 0.5%p"), R("NH농협은행")]]}]


def observe_pages(meta, schema, text):
    ids = _enum(schema)
    if ids[0].startswith("P1"):
        return {"targets": [{"tid": "t1", "name": "NH올원e통장", "group": "예금성", "templates": ["deposit_demand"],
                             "evidence": [{"feature": "deposit_demand.free", "region": "P1-R02", "quote": "입출금이 자유로운"}]}],
                "mentions": [], "regions": {a: {"scope": "specific", "targets": ["t1"]} for a in ids}, "parts": []}
    return {"targets": [{"tid": "t1", "name": None, "group": "예금성", "templates": ["deposit_demand"],
                         "evidence": [{"feature": "deposit_demand.daily_balance", "region": "P2-R01", "quote": "우대금리 연 0.5%p"}]}],
            "mentions": [], "regions": {"P2-R01": {"scope": "specific", "targets": ["t1"]},
                                        "P2-R02": {"scope": "shared", "targets": []}}, "parts": []}


def test_paged_flow_links_same_product_across_files(tmp_path, monkeypatch):
    _paged(monkeypatch)
    seen = []

    def integrate(meta, schema, text):
        seen.append(text)
        return {"targets": {"T1": {"product": "p1"}, "T2": {"product": "p1"}}, "products": [{"product": "p1", "templates": ["deposit_demand"]}]}
    fake = Fake(observe=observe_pages, integrate=integrate, label=EMPTY_LABELS)
    rep, out = run_fake(tmp_path, TWO_FILES, fake)
    assert rep["checks"]["passed"] and rep["submissions"][0]["mode"] == "paged"
    d1, d2 = load(out / "files" / "b01" / "result.json"), load(out / "files" / "b02" / "result.json")
    assert d1["products"] == d2["products"] and d1["products"][0]["files"] == ["b01", "b02"]
    assert d1["products"][0]["name"] == "NH올원e통장"
    assert d2["regions"][1]["product_assignment"] == {"status": "completed", "scope": "shared", "product_ids": ["product_001"],
                                                      "basis": "model"}
    assert "x.png" not in seen[0] and "b01" not in seen[0] and "T1" in seen[0] and "T2" in seen[0]
    assert sorted(k.split(":")[0] for k in fake.keys).count("integrate") == 1
    # 심의 단계 입력(review-input/2): 묶음 하나에 두 파일의 페이지·영역이 입력 순서로 들어가고 값은 원래 결과 그대로
    h = load(out / "handoff" / f"{d1['submission_id']}.json")
    assert rep["handoff"]["passed"] and h["contract"] == "review-input/2"
    assert [f["file_id"] for f in h["files"]] == ["b01", "b02"] and [p["file_id"] for p in h["pages"]] == ["b01", "b02"]
    assert [p["seq"] for p in h["pages"]] == [1, 2] and [p["page_no"] for p in h["pages"]] == [1, 1]   # 묶음 연번과 파일 안 번호
    assert h["template_request"] == {"template_ids": [], "mode": "auto"} and h["files"][0]["hwp_review_html"] is None
    assert {h["deposit_insurance_marks"][k]["status"] for k in ("protected", "non_protected")} == {"ABSENT"}
    assert [p["product_id"] for p in h["products"]] == ["product_001"] and h["products"][0]["templates"][0]["labels"]
    src = {r["region_id"]: r for d in (d1, d2) for r in d["regions"]}
    got = {r["region_id"]: r for p in h["pages"] for r in p["regions"]}
    assert set(got) == set(src) and all(got[k]["text"] == src[k]["text"] and got[k]["bbox"] == src[k]["bbox"] for k in got)
    assert got[d2["regions"][1]["region_id"]]["product"] == {"scope": "shared", "product_ids": ["product_001"]}


def test_handoff_keeps_table_cells_and_marks_missing_structure():
    """표는 셀 구조 그대로, 셀 구조가 없는 표는 table=null + 검토. 맥락 사유(경계·보충)는 needs_review 를 켜지 않는다."""
    from layout_pipeline import handoff
    cells = [{"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "구분"}, {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "기간"},
             {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "12개월"}]

    def region(rid, table, quality=(), context=()):
        return {"region_id": rid, "page_id": "a_p01", "bbox": [0, 0, 10, 10], "text": "구분 기간 12개월", "text_status": "read",
                "table": table, "review": {"required": bool(quality or context), "quality_reasons": list(quality),
                                           "context_reasons": list(context)},
                "product_assignment": {"status": "completed", "scope": "specific", "product_ids": ["product_001"], "basis": "model"},
                "labeling": {"status": "completed", "items": [{"product_id": "product_001", "label_ids": ["가입기간"]}]},
                "semantic_review": {"required": False, "codes": []}}
    ok = handoff._region(region("r1", {"status": "table", "rows": [], "structure": "table-cells/1", "n_rows": 2, "n_cols": 2,
                                       "cells": cells}, context=["candidate_boundary_unvalidated"]))
    assert ok["is_table"] and ok["table"] == {"n_rows": 2, "n_cols": 2, "cells": cells} and not ok["needs_review"]
    bad = handoff._region(region("r2", {"status": "table", "rows": [["기간", "12개월"]], "structure": "rows/legacy"},
                                 quality=["table_structure_failed"]))
    assert bad["is_table"] and bad["table"] is None and bad["needs_review"] and bad["quality_reasons"] == ["table_structure_failed"]
    plain = handoff._region(region("r3", {"status": "not_table", "rows": [], "structure": "table-cells/1"}))
    assert plain["is_table"] is False and plain["table"] is None and plain["text_source"] == "vlm"


def test_handoff_separates_quality_from_semantic_codes_and_marks_hwp_text():
    """review-input/2(docs/21): 의미 단계 사유만 있는 영역은 판독 품질 사유가 비어 있다(판정이 판독 불가로 보지 않게).
    HWP 구조 영역(-h0-H)은 문구 출처가 hwp, 문구가 없으면 null."""
    from layout_pipeline import handoff
    base = {"page_id": "a_p01", "bbox": [0, 0, 10, 10], "table": None,
            "review": {"required": False, "quality_reasons": [], "context_reasons": ["region_overlap"]},
            "product_assignment": {"status": "partial", "scope": "unknown", "product_ids": [], "basis": "model"},
            "labeling": {"status": "not_run", "items": []},
            "semantic_review": {"required": True, "codes": ["assignment_unknown"]}}
    r = handoff._region(base | {"region_id": "a_p01-v0-L000", "text": "공통 안내", "text_status": "read"})
    assert r["quality_reasons"] == [] and r["semantic_codes"] == ["assignment_unknown"] and r["needs_review"]
    hwp = handoff._region(base | {"region_id": "a_p01-h0-H003", "text": "가입기간 12개월", "text_status": "read"})
    assert hwp["text_source"] == "hwp"
    failed = handoff._region(base | {"region_id": "a_p01-v0-L001", "text": None, "text_status": "read_failed"})
    assert failed["text_source"] is None and failed["quality_reasons"] == ["read_failed"]


def test_integration_failure_keeps_targets_separate_with_review(tmp_path, monkeypatch):
    _paged(monkeypatch)
    fake = Fake(observe=observe_pages, integrate=lambda m, s, t: None,
                label=EMPTY_LABELS)
    rep, out = run_fake(tmp_path, TWO_FILES, fake)
    d1 = load(out / "files" / "b01" / "result.json")
    assert rep["checks"]["passed"] and d1["semantic"]["status"] == "partial"
    prods = {p["product_id"]: p for f in ("b01", "b02") for p in load(out / "files" / f / "result.json")["products"]}
    # first-input/1(docs/20): 뒤 입력의 이름 없는 대상은 통합 실패여도 첫 입력 상품에 잇는다(검토 코드 유지)
    assert len(prods) == 1 and {"integration_failed", "unnamed_attached"} <= set(prods["product_001"]["review"]["codes"])


def test_failed_observation_marks_regions_failed_not_empty(tmp_path, monkeypatch):
    _paged(monkeypatch)

    def observe(meta, schema, text):
        return None if _enum(schema)[0].startswith("P2") else observe_pages(meta, schema, text)
    fake = Fake(observe=observe, integrate=lambda m, s, t: {"targets": {"T1": {"product": "p1"}}, "products": [{"product": "p1", "templates": ["deposit_demand"]}]},
                label=EMPTY_LABELS)
    rep, out = run_fake(tmp_path, TWO_FILES, fake)
    d2 = load(out / "files" / "b02" / "result.json")
    assert rep["checks"]["passed"] and d2["semantic"]["status"] == "partial"
    for r in d2["regions"]:
        assert r["product_assignment"] == {"status": "failed", "scope": None, "product_ids": [], "basis": None}
        assert r["labeling"] == {"status": "not_run", "items": []} and "assignment_failed" in r["semantic_review"]["codes"]


def test_label_call_failure_is_failed_not_empty(tmp_path):
    rep, out = run_fake(tmp_path, DEPOSIT, Fake(observe=observe_one, label=lambda m, s, t: None))
    d = load(out / "files" / "a01" / "result.json")
    r = d["regions"][1]
    assert r["labeling"] == {"status": "failed", "items": []} and "label_failed" in r["semantic_review"]["codes"]
    assert d["regions"][3]["labeling"]["status"] == "completed"           # 글자 없음 영역은 호출 없이 라벨 없음 확정


# ---- 상품 판단 검토 코드와 퇴직연금 ----

def _bundle(tmp, pages, unassigned=None, status="READY"):
    files = [{"sub": "p", "fid": "c01", "name": "z.pdf", "kind": "pdf", "pages": pages, "status": status,
              "unassigned": unassigned or [[] for _ in pages]}]
    docs, paths = semantic.load_parse(make_parse(tmp, files))
    return classify.build_bundle(docs, paths)


def _units(b, targets, scope="specific"):
    """페이지마다 관측 단위 하나. targets: {page_alias: [target...]}"""
    units = []
    for pa in b["page_order"]:
        ts = targets.get(pa, [])
        for t in ts:
            for e in t["evidence"]:
                e["valid"] = classify.quote_where(e["quote"], b["regions"][e["region"]]) is not None
        regions = {a: {"scope": scope if ts else "shared", "targets": [ts[0]["tid"]] if ts else [], "note": None}
                   for a in b["pages"][pa]["regions"]}
        units.append({"unit": pa, "pages": [pa], "obs": {"ok": True, "targets": ts, "regions": regions, "parts": [], "missing": [],
                                                       "mentions": [], "errors": []}})
    return units


def _t(name, codes, ev, group="투자성"):
    return {"tid": "t1", "name": name, "group": group, "templates": codes,
            "evidence": [{"feature": f, "region": r, "quote": q} for f, r, q in ev]}


PENSION_PAGES = [[R("퇴직연금 IRP 가입하고 혜택 받으세요")], [R("디폴트옵션 안내")], [R("ETF 라인업: KODEX 200")]]


@pytest.mark.parametrize("ev_last,expect,comps,code", [
    ([("pension.option_etf", "P3-R01", "ETF 라인업")], "included", ["ETF"], None),                     # 마지막 페이지에만 ETF
    ([("pension.notice_investment", "P3-R01", "ETF 라인업")], "not_exposed", [], None),                 # 고지·범주명 근거만
    ([("pension.option_other", "P3-R01", "KODEX 200")], "included", [], "pension_other_investment_only"),
])
def test_pension_exposure_uses_all_pages(tmp_path, ev_last, expect, comps, code):
    b = _bundle(tmp_path, PENSION_PAGES)
    units = _units(b, {"P1": [_t("퇴직연금", ["pension"], [("pension.common", "P1-R01", "퇴직연금"), ("pension.plan_irp", "P1-R01", "IRP")])],
                       "P3": [_t(None, ["pension"], ev_last)]})
    integ = {"ok": True, "products": [{"members": ["T1", "T2"], "templates": ["pension"]}], "missing": [], "errors": []}
    p = classify.resolve(b, units, integ)["products"][0]
    assert p["pension"]["investment_products"] == expect and p["pension"]["components"] == comps and p["pension"]["plans"] == ["IRP"]
    assert (code in p["review"]) if code else not any(c.startswith("pension_") for c in p["review"])


def test_pension_fund_and_etf_together_and_order(tmp_path):
    b = _bundle(tmp_path, [[R("퇴직연금 TDF 펀드와 ETF 함께 운용")]])
    units = _units(b, {"P1": [_t("퇴직연금", ["pension"], [("pension.option_etf", "P1-R01", "ETF"), ("pension.option_fund", "P1-R01", "TDF 펀드")])]})
    p = classify.resolve(b, units, None)["products"][0]
    assert p["pension"]["components"] == ["FUND", "ETF"]


def test_pension_not_exposed_is_not_confirmed_with_failed_or_unassigned_evidence(tmp_path):
    ev = [("pension.common", "P1-R01", "퇴직연금")]
    b = _bundle(tmp_path / "a", [[R("퇴직연금 IRP")], [R(None)]])                 # 판독 실패 영역이 있는 페이지
    p = classify.resolve(b, _units(b, {"P1": [_t("퇴직연금", ["pension"], ev)]}), None)["products"][0]
    assert p["pension"]["investment_products"] == "undetermined" and "pension_pages_incomplete" in p["review"]
    b = _bundle(tmp_path / "b", [[R("퇴직연금 IRP")]], unassigned=[["ETF 상품 라인업"]])   # 영역 밖 OCR 에 운용상품 용어
    p = classify.resolve(b, _units(b, {"P1": [_t("퇴직연금", ["pension"], ev)]}), None)["products"][0]
    assert p["pension"]["investment_products"] == "undetermined" and "pension_unassigned_ocr_terms" in p["review"]
    b = _bundle(tmp_path / "c", [[R("퇴직연금 IRP")]])
    p = classify.resolve(b, _units(b, {"P1": [_t("퇴직연금", ["pension"], ev)]}), None)["products"][0]
    assert p["pension"]["investment_products"] == "not_exposed"


@pytest.mark.parametrize("codes,ev,text,expect", [
    (["loan_broker", "loan_named"], [("loan_broker.decisive", "P1-R01", "대출상담사"), ("loan_named.name", "P1-R01", "NH주택담보대출")],
     "대출상담사 홍길동 NH주택담보대출 최저 연 3%", "broker_with_named_loan"),
    (["isa_general", "isa_trust"], [("isa_general.unspecified", "P1-R01", "ISA"), ("isa_trust.name", "P1-R01", "신탁형 ISA")],
     "ISA 신탁형 ISA", "template_conflict"),
    (["deposit_demand", "deposit_saving"], [("deposit_demand.name", "P1-R01", "통장"), ("deposit_saving.name", "P1-R01", "적금")],
     "통장 적금", "multi_template_product"),
    (["isa_trust"], [("isa_general.common", "P1-R01", "ISA")], "ISA 계좌", "template_evidence_missing"),
    (["loan_named"], [("loan_named.name", "P1-R01", "NH전세대출")], "NH전세대출 대출모집법인 ○○", "broker_signal_unused"),
])
def test_template_review_codes(tmp_path, codes, ev, text, expect):
    b = _bundle(tmp_path, [[R(text)]])
    p = classify.resolve(b, _units(b, {"P1": [_t(None, codes, ev, "대출성")]}), None)["products"][0]
    assert expect in p["review"]


def test_name_must_be_in_final_text(tmp_path):
    b = _bundle(tmp_path, [[R("NH튼튼적금 안내")]])
    ok = classify.resolve(b, _units(b, {"P1": [_t("NH튼튼적금", ["deposit_saving"], [("deposit_saving.name", "P1-R01", "NH튼튼적금")])]}), None)
    assert ok["products"][0]["name"] == "NH튼튼적금"
    bad = classify.resolve(b, _units(b, {"P1": [_t("NH든든적금", ["deposit_saving"], [("deposit_saving.name", "P1-R01", "NH튼튼적금")])]}), None)
    assert bad["products"][0]["name"] is None and "name_not_in_text" in bad["products"][0]["review"]


def test_unsupported_and_undetermined_products_are_not_labeled(tmp_path):
    def observe(meta, schema, text):
        ids = _enum(schema)
        return {"targets": [{"tid": "t1", "name": None, "group": "카드", "templates": ["unsupported"],
                             "evidence": [{"feature": "other.product", "region": "P1-R03", "quote": "가입기간 12개월"}]}],
                "mentions": [], "regions": {a: {"scope": "specific", "targets": ["t1"]} for a in ids}, "parts": []}
    fake = Fake(observe=observe, label=label_scripted)
    rep, out = run_fake(tmp_path, DEPOSIT, fake)
    d = load(out / "files" / "a01" / "result.json")
    assert rep["checks"]["passed"] and d["products"][0]["status"] == "unsupported" and d["products"][0]["templates"] == []
    assert not any(k.startswith("label:") for k in fake.keys)
    assert all(r["labeling"]["status"] == "not_run" for r in d["regions"])
    assert "product_not_classified" in d["regions"][1]["semantic_review"]["codes"]


# ---- 계약·재구성·동시성 ----

def _valid_doc(tmp_path):
    rep, out = run_fake(tmp_path, DEPOSIT, Fake(observe=observe_one, label=label_scripted))
    return load(out / "files" / "a01" / "result.json")


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["regions"][1]["product_assignment"]["product_ids"].append("product_009"), "products 에 없는"),
    (lambda d: d["regions"][1]["labeling"]["items"][0]["label_ids"].append("수수료"), "허용 라벨 밖"),
    (lambda d: d["regions"][1]["labeling"]["items"].append({"product_id": "product_002", "label_ids": []}), "영역 product_ids 에 없음"),
    (lambda d: d["regions"][1]["product_assignment"].update(scope="mixed"), "mixed 인데 상품 1개"),
    (lambda d: d["regions"][1]["labeling"].update(items=[]), "schema"),
    (lambda d: d["regions"][1]["semantic_review"].update(required=True), "required 와 codes"),
    (lambda d: d["regions"][4]["labeling"].update(status="completed", items=[{"product_id": "product_001", "label_ids": []}]),
     "배정이 완료되지 않았는데"),
    (lambda d: d["products"][0].update(pension={"investment_products": "included", "plans": [], "components": []}), "pension"),
    (lambda d: d["products"][0].update(status="unsupported"), "schema"),
    (lambda d: d["regions"][0]["product_assignment"].update(basis="single_product"), "schema"),        # shared 영역
    (lambda d: d["regions"][1]["product_assignment"].update(basis=None), "schema"),                     # completed 는 basis 필요
    (lambda d: d["products"][0].update(pages=["a01_p01"]), "pages 에 이 영역 페이지가 없음"),
    (lambda d: d["products"][0].update(pages=["x99_p01"]), "pages 에 이 문서 페이지가 없음"),
])
def test_contract_v3_rejects_violations(tmp_path, mutate, expect):
    d = copy.deepcopy(_valid_doc(tmp_path))
    assert contract_v3.check_document(d) == []
    mutate(d)
    errs = contract_v3.check_document(d)
    assert errs and any(expect in e for e in errs), errs


def test_preservation_and_submission_consistency(tmp_path):
    d = _valid_doc(tmp_path)
    p3, p2 = tmp_path / "sem" / "files" / "a01" / "result.json", tmp_path / "parse" / "files" / "a01" / "result.json"
    v2 = load(p2)
    assert contract_v3.check_preservation(d, p3, v2, p2) == []
    for mut, key in ((lambda x: x["regions"][0].update(text="바뀐 문구"), "text"), (lambda x: x["regions"][0].update(bbox=[0, 0, 1, 1]), "bbox"),
                     (lambda x: x["unassigned_texts"].append({}), "unassigned"), (lambda x: x["regions"].pop(), "영역 목록")):
        x = copy.deepcopy(d)
        mut(x)
        assert any(key in e for e in contract_v3.check_preservation(x, p3, v2, p2))
    other = copy.deepcopy(d) | {"source": d["source"] | {"file_id": "a02", "input_order": 2}}
    other["products"] = copy.deepcopy(d["products"])
    other["products"][0]["name"] = "다른 이름"
    assert any("정의가 파일마다 다름" in e for e in contract_v3.check_submission([d, other]))
    stray = copy.deepcopy(d)
    stray["products"][0]["pages"] = stray["products"][0]["pages"] + ["zz_p09"]
    assert any("묶음에 없는 페이지" in e for e in contract_v3.check_submission([stray]))


def test_rebuild_replays_same_result_without_calls(tmp_path):
    fake = Fake(observe=observe_one, label=label_scripted)
    rep, out = run_fake(tmp_path, DEPOSIT, fake)
    before = (out / "files" / "a01" / "result.json").read_bytes()
    n = fake.count
    again = semantic.rebuild(out)
    assert fake.count == n and again["timing"]["replay"] and again["timing"]["http_calls"] == n
    assert (out / "files" / "a01" / "result.json").read_bytes() == before
    v2 = tmp_path / "parse" / "files" / "a01" / "result.json"            # 입력(최종 문구)이 바뀌면 재구성을 멈춘다
    doc = load(v2)
    doc["regions"][1]["text"] = "NH다른적금"
    dump(v2, doc)
    with pytest.raises(ValueError):
        semantic.rebuild(out)


def test_report_html_covers_every_region_and_reads_run_timing(tmp_path):
    fake = Fake(observe=observe_one, label=label_scripted)
    rep, out = run_fake(tmp_path, DEPOSIT, fake)
    page = out / "report.html"
    assert rep["report_html"] == str(page) and page.is_file()
    html = page.read_text(encoding="utf-8")
    d = load(out / "files" / "a01" / "result.json")
    assert html.count("<li class='region'") == len(d["regions"])
    for r in d["regions"]:
        assert f"data-rid='{r['region_id']}'" in html                                     # 영역마다 목록 항목과 상자
    assert "<span class='chip rev'>소속 미확정</span>" in html and "판독 실패 · 최종 문구 없음 (read_failed)" in html
    assert "class='grid'" in html and "회사명" in html and "라벨 근거 인용" in html
    src = re.search(r"<img [^>]*src='([^']+)'", html).group(1)
    assert (out / unquote(src)).is_file()                                                 # 이미지 상대 경로가 열린다
    rj = load(out / "report.json")                                                        # 시간은 실행 당시 기록(report.json)
    rj["submissions"][0]["timing"]["total_wall_seconds"] = 37.875
    dump(out / "report.json", rj)
    assert "37.9초" in build_report(out).read_text(encoding="utf-8")
    semantic.rebuild(out)                                                                 # 재구성은 report.json 을 덮어쓰지 않는다
    again = page.read_text(encoding="utf-8")
    assert "37.9초" in again and "실제 호출 시간이 아니다" not in again
    (out / "report.json").unlink()                                                        # 원본이 없으면 재생 기록을 경고와 함께
    assert "실제 호출 시간이 아니다" in build_report(out).read_text(encoding="utf-8")


def test_gate_keeps_total_concurrency_within_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(labeling, "LABEL_MAX_REGIONS", 1)               # 라벨 작업을 여러 개로 나눠 병렬로 보낸다
    fake = Fake(observe=observe_one, label=label_scripted)
    fake.delay = 0.05
    rep, _ = run_fake(tmp_path, DEPOSIT, fake, concurrency=2)
    assert rep["timing"]["peak_concurrency"] <= 2 and fake.peak <= 2 and rep["timing"]["http_calls"] >= 6


def test_target_without_name_or_evidence_is_dropped(tmp_path):
    """근거 인용도 영역 문구 속 이름도 없는 대상은 상품으로 등록하지 않는다. 그 대상 전용 영역은 관측에 남은 대상이 하나뿐이면
    그 대상으로 채우고(docs/19) 검토 코드는 남긴다."""
    def observe(meta, schema, text):
        out = observe_one(meta, schema, text)
        out["targets"].append({"tid": "t2", "name": None, "group": "기타", "templates": ["unsupported"], "evidence": []})
        out["regions"]["P1-R01"] = {"scope": "shared", "targets": ["t1", "t2"]}
        out["regions"]["P1-R04"] = {"scope": "specific", "targets": ["t2"]}
        return out
    rep, out = run_fake(tmp_path, DEPOSIT, Fake(observe=observe, label=label_scripted))
    d = load(out / "files" / "a01" / "result.json")
    assert rep["checks"]["passed"] and [p["product_id"] for p in d["products"]] == ["product_001"]
    assert d["regions"][0]["product_assignment"]["product_ids"] == ["product_001"]
    assert d["regions"][3]["product_assignment"] == {"status": "completed", "scope": "specific", "product_ids": ["product_001"],
                                                     "basis": "model"}
    assert "target_without_evidence" in d["regions"][3]["semantic_review"]["codes"]
    trace = load(out / "trace" / "semantic" / "s1.json")
    assert trace["units"][0]["dropped_targets"][0]["tid"] == "t2"


def test_isa_general_with_type_products_in_same_file_is_flagged(tmp_path):
    b = _bundle(tmp_path, [[R("ISA 소개 신탁형 ISA와 일임형 ISA의 차이점")]])
    ts = [_t("ISA", ["isa_general"], [("isa_general.both_types", "P1-R01", "신탁형 ISA와 일임형 ISA의 차이점")]),
          _t("신탁형 ISA", ["isa_trust"], [("isa_trust.name", "P1-R01", "신탁형 ISA")])]
    ts[1]["tid"] = "t2"
    units = _units(b, {"P1": ts})
    units[0]["obs"]["regions"]["P1-R01"]["targets"] = ["t1", "t2"]
    units[0]["obs"]["regions"]["P1-R01"]["scope"] = "mixed"
    prods = classify.resolve(b, units, None)["products"]
    assert len(prods) == 2 and all("isa_types_split" in p["review"] for p in prods)


def test_pension_product_across_files_with_different_exposure_is_flagged(tmp_path):
    files = [{"sub": "p", "fid": "d01", "name": "a.png", "kind": "image", "pages": [[R("IRP 고객감사 이벤트")]]},
             {"sub": "p", "fid": "d02", "name": "b.pdf", "kind": "pdf", "pages": [[R("퇴직연금 ETF 라인업")]]}]
    docs, paths = semantic.load_parse(make_parse(tmp_path, files))
    b = classify.build_bundle(docs, paths)
    units = _units(b, {"P1": [_t("IRP", ["pension"], [("pension.plan_irp", "P1-R01", "IRP")])],
                       "P2": [_t(None, ["pension"], [("pension.option_etf", "P2-R01", "ETF 라인업")])]})
    integ = {"ok": True, "products": [{"members": ["T1", "T2"], "templates": ["pension"]}], "missing": [], "errors": []}
    p = classify.resolve(b, units, integ)["products"][0]
    assert p["pension"]["investment_products"] == "included" and "pension_exposure_differs_by_file" in p["review"]


def test_integration_request_shows_short_title_candidates_without_cutting(tmp_path, monkeypatch):
    _paged(monkeypatch)
    seen = []

    def integrate(meta, schema, text):
        seen.append(text)
        return {"targets": {"T1": {"product": "p1"}, "T2": {"product": "p1"}}, "products": [{"product": "p1", "templates": ["deposit_demand"]}]}
    run_fake(tmp_path, TWO_FILES, Fake(observe=observe_pages, integrate=integrate, label=EMPTY_LABELS))
    assert '제목 후보: ["NH올원e통장", "입출금이 자유로운 통장"]' in seen[0]


def test_integration_product_without_templates_uses_member_proposals_with_review(tmp_path, monkeypatch):
    _paged(monkeypatch)
    integ = lambda m, s, t: {"targets": {"T1": {"product": "p1"}, "T2": {"product": "p2"}}, "products": [{"product": "p1", "templates": ["deposit_demand"]}]}  # noqa: E731
    rep, out = run_fake(tmp_path, TWO_FILES, Fake(observe=observe_pages, integrate=integ, label=EMPTY_LABELS))
    prods = {p["product_id"]: p for f in ("b01", "b02") for p in load(out / "files" / f / "result.json")["products"]}
    # first-input/1: 뒤 입력의 이름 없는 대상(p2)은 첫 입력 상품에 이어지고 그 대상의 검토 코드도 함께 남는다
    assert rep["checks"]["passed"] and len(prods) == 1
    p1 = prods["product_001"]
    assert p1["files"] == ["b01", "b02"] and p1["templates"][0]["template_id"] == "예금성상품-입출식"
    assert {"integration_templates_missing", "unnamed_attached"} <= set(p1["review"]["codes"])


def test_broker_template_needs_decisive_word_in_its_quote(tmp_path):
    """decisive-signal/1(docs/19): 대출모집인 결정 신호로 인용한 문구에 결정 신호 낱말이 없으면 템플릿을 뺀다. 대신 두는 템플릿은
    상품명이 있으면 상품명 노출, 없으면 상품명 미노출(classification-policy/4, docs/21)."""
    b = _bundle(tmp_path, [[R("검단 중도금대출 강남중앙금융센터 류지선 과장 대출금리 연 3.87%")]])
    ev = [("loan_named.name", "P1-R01", "검단 중도금대출"), ("loan_broker.decisive", "P1-R01", "강남중앙금융센터 류지선 과장")]
    p = classify.resolve(b, _units(b, {"P1": [_t(None, ["loan_named", "loan_broker"], ev, "대출성")]}), None)["products"][0]
    assert p["codes"] == ["loan_named"] and "decisive_signal_missing" in p["review"]
    assert "named_template_without_name" in p["review"]          # 모델이 고른 노출 템플릿은 그대로 두고 표시만
    only = classify.resolve(b, _units(b, {"P1": [_t(None, ["loan_broker"], ev[1:], "대출성")]}), None)["products"][0]
    assert only["codes"] == ["loan_unnamed"] and only["status"] == "classified"
    named = classify.resolve(b, _units(b, {"P1": [_t("검단 중도금대출", ["loan_broker"], ev[1:], "대출성")]}), None)["products"][0]
    assert named["codes"] == ["loan_named"] and named["name"] == "검단 중도금대출"
    b2 = _bundle(tmp_path / "b", [[R("대출상담사 홍길동 NH주택담보대출")]])
    ok = classify.resolve(b2, _units(b2, {"P1": [_t(None, ["loan_broker"], [("loan_broker.decisive", "P1-R01", "대출상담사 홍길동")], "대출성")]}),
                          None)["products"][0]
    assert ok["codes"] == ["loan_broker"] and "decisive_signal_missing" not in ok["review"]


def _files_bundle(tmp, pages_per_file):
    """파일마다 1쪽(영역 문구 목록)인 묶음 하나."""
    files = [{"sub": "m", "fid": f"m{i + 1:02d}", "name": f"{i + 1}.png", "kind": "image", "pages": [regs], "status": "READY",
              "unassigned": [[]]} for i, regs in enumerate(pages_per_file)]
    docs, paths = semantic.load_parse(make_parse(tmp, files))
    return classify.build_bundle(docs, paths)


def _named(tid, name, codes, ev, group="예금성"):
    return {"tid": tid, "name": name, "group": group, "templates": codes,
            "evidence": [{"feature": f, "region": r, "quote": q} for f, r, q in ev]}


def test_first_input_rules_merge_same_name_keep_first_template_and_attach_unnamed(tmp_path):
    """first-input/1(docs/20, whole-test 거치식 9장): 띄어쓰기만 다른 같은 이름은 한 상품, 템플릿은 첫 입력 판정, 뒤 입력의 이름 없는
    대상은 첫 입력 상품에 잇는다. 뒤 입력에만 있는 이름 있는 다른 상품은 그대로 둔다."""
    b = _files_bundle(tmp_path, [[R("NH동행예금"), R("가입기간 12개월")], [R("NH동행 예금 금리표")], [R("유의사항")], [R("NH튼튼적금")]])
    units = _units(b, {"P1": [_named("t1", "NH동행예금", ["deposit_term"], [("deposit_term.name", "P1-R01", "NH동행예금")])],
                       "P2": [_named("t1", "NH동행 예금", ["deposit_saving"], [("deposit_saving.name", "P2-R01", "NH동행 예금")])],
                       "P3": [_named("t1", None, ["undetermined"], [("other.product", "P3-R01", "유의사항")])],
                       "P4": [_named("t1", "NH튼튼적금", ["deposit_saving"], [("deposit_saving.name", "P4-R01", "NH튼튼적금")])]})
    res = classify.resolve(b, units, {"ok": True, "products": [{"members": [t], "templates": [c], "codes": []} for t, c in
                                                               (("T1", "deposit_term"), ("T2", "deposit_saving"), ("T3", "undetermined"),
                                                                ("T4", "deposit_saving"))], "missing": []})
    p1, p2 = res["products"]
    assert p1["codes"] == ["deposit_term"] and {"merged_same_name", "unnamed_attached"} <= set(p1["review"])
    assert p1["files"] == [f["file_id"] for f in b["files"][:3]] and p2["name"] == "NH튼튼적금"
    assert [r["rule"] for r in res["merge_rules"]] == ["M1", "M2_unnamed"]
    # 통합이 첫 입력 대상과 뒤 입력 대상을 한 상품으로 묶으며 다른 템플릿을 골라도 첫 입력 대상의 템플릿을 쓴다
    res2 = classify.resolve(b, units, {"ok": True, "missing": [], "products": [
        {"members": ["T1", "T2", "T3"], "templates": ["deposit_saving"], "codes": []},
        {"members": ["T4"], "templates": ["deposit_saving"], "codes": []}]})
    assert res2["products"][0]["codes"] == ["deposit_term"] and "template_from_first_input" in res2["products"][0]["review"]


def test_r3_weak_unnamed_target_merges_into_only_named_target():
    """R3(docs/20, whole-test 적립식 광고 2): 이름·상품명·유형 근거 없이 영역 하나의 문구만 근거인 대상은 유일한 이름 있는 대상에
    합친다. 상품명·유형 근거가 있거나 이름 있는 대상이 둘이면 그대로 둔다."""
    def obs(t2_ev, named2=False):
        ts = [{"tid": "t1", "name": "NH대박적금", "group": "예금성", "templates": ["deposit_saving"],
               "evidence": [{"feature": "deposit_saving.name", "region": "P1-R01", "quote": "NH대박적금", "valid": True}]},
              {"tid": "t2", "name": None, "group": "투자성", "templates": ["pension"], "evidence": t2_ev}]
        if named2:
            ts.append({"tid": "t3", "name": "NH든든예금", "group": "예금성", "templates": ["deposit_term"], "evidence": []})
        return {"ok": True, "errors": [], "mentions": [], "parts": [], "missing": [], "targets": ts,
                "regions": {"P1-R01": {"scope": "specific", "targets": ["t1"], "note": None},
                            "P1-R02": {"scope": "specific", "targets": ["t2"], "note": None}}}
    weak = [{"feature": "pension.notice_investment", "region": "P1-R02", "quote": "이 예금은 예금자보호법에 따라", "valid": True}]
    out = classify.apply_target_rules(obs(weak))
    assert [t["tid"] for t in out["targets"]] == ["t1"] and out["regions"]["P1-R02"]["targets"] == ["t1"]
    assert out["target_rules"][-1]["rule"] == "R3"
    strong = weak + [{"feature": "pension.plan_irp", "region": "P1-R03", "quote": "IRP", "valid": True}]
    assert len(classify.apply_target_rules(obs(strong))["targets"]) == 2
    assert len(classify.apply_target_rules(obs(weak, named2=True))["targets"]) == 3


# ---- 등록 템플릿 요청·상품명 미노출·예금자보호 표시(docs/21) ----

def test_template_request_modes_and_unknown_template():
    assert classify.template_request(None) == {"template_ids": [], "codes": [], "mode": "auto"}
    assert classify.template_request(["예금성상품-적립식"])["codes"] == ["deposit_saving"]
    assert classify.template_request(["예금성상품-적립식"])["mode"] == "fixed"
    assert classify.template_request(["예금성상품-입출식", "예금성상품-적립식"])["mode"] == "restricted"
    assert A.request_codes(["대출성상품-상품명 미노출", "예금성상품-상품명 미노출"]) == ["loan_unnamed", "deposit_unnamed"]
    with pytest.raises(ValueError):
        classify.template_request(["카드상품-상품명 미노출"])           # 이 파이프라인이 분류하지 않는 템플릿


def test_fixed_request_puts_the_registered_template_on_every_product(tmp_path):
    """등록 템플릿이 하나면 모델이 고른 템플릿(범위 밖 포함)과 상관없이 모든 상품에 그 템플릿을 쓰고 다르면 표시한다."""
    b = _bundle(tmp_path, [[R("NH올원e통장 입출금이 자유로운"), R("NH튼튼적금 월 10만원")]])
    b["request"] = classify.template_request(["예금성상품-적립식"])
    t1 = _t("NH올원e통장", ["deposit_demand"], [("deposit_demand.name", "P1-R01", "NH올원e통장")], "예금성")
    t2 = _t("NH튼튼적금", ["deposit_saving"], [("deposit_saving.name", "P1-R02", "NH튼튼적금")], "예금성") | {"tid": "t2"}
    units = _units(b, {"P1": [t1, t2]})
    units[0]["obs"]["regions"]["P1-R02"]["targets"] = ["t2"]
    prods = classify.resolve(b, units, None)["products"]
    assert [p["codes"] for p in prods] == [["deposit_saving"], ["deposit_saving"]] and all(p["status"] == "classified" for p in prods)
    assert "template_from_request" in prods[0]["review"] and "template_from_request" not in prods[1]["review"]
    broker = _bundle(tmp_path / "b", [[R("검단 중도금대출 강남중앙금융센터 류지선 과장")]])
    broker["request"] = classify.template_request(["대출성상품-대출모집인(잔금대출)"])
    ev = [("loan_broker.decisive", "P1-R01", "강남중앙금융센터 류지선 과장")]
    p = classify.resolve(broker, _units(broker, {"P1": [_t(None, ["loan_broker"], ev, "대출성")]}), None)["products"][0]
    assert p["codes"] == ["loan_broker"] and "decisive_signal_missing" not in p["review"]     # 사용자 지정은 결정 신호 필터 밖


def test_restricted_request_limits_prompt_schema_and_keeps_registered_choice(tmp_path):
    b = _bundle(tmp_path, [[R("검단 중도금대출 강남중앙금융센터 류지선 과장"), R("NH튼튼적금")]])
    b["request"] = classify.template_request(["대출성상품-대출모집인(잔금대출)", "예금성상품-적립식"])
    req = classify.observe_request(b, b["page_order"], True)
    enum = req["schema"]["properties"]["targets"]["items"]["properties"]["templates"]["items"]["enum"]
    assert enum == ["loan_broker", "deposit_saving", "unsupported", "undetermined"]
    assert "[등록 템플릿]" in req["text"] and "- loan_broker (" in req["text"] and "- deposit_term (" not in req["text"]
    feats = req["schema"]["properties"]["targets"]["items"]["properties"]["evidence"]["items"]["properties"]["feature"]["enum"]
    assert all(f.split(".")[0] in ("loan_broker", "deposit_saving", "other") for f in feats)
    auto = classify.observe_request(_bundle(tmp_path / "a", [[R("NH튼튼적금")]]), ["P1"], True)
    assert "[등록 템플릿]" not in auto["text"] and "deposit_term" in json.dumps(auto["schema"])
    # 결정 신호가 없어도 대체 템플릿(loan_named·loan_unnamed)이 등록 목록 밖이면 등록 템플릿을 그대로 두고 표시만 한다
    ev = [("loan_broker.decisive", "P1-R01", "강남중앙금융센터 류지선 과장")]
    p = classify.resolve(b, _units(b, {"P1": [_t(None, ["loan_broker"], ev, "대출성")]}), None)["products"][0]
    assert p["codes"] == ["loan_broker"] and "decisive_signal_missing" in p["review"]


def test_unnamed_templates_are_classified_and_labeled_with_their_schema(tmp_path):
    def observe(meta, schema, text):
        out = observe_one(meta, schema, text)
        out["targets"][0] |= {"name": None, "templates": ["deposit_unnamed"],
                              "evidence": [{"feature": "deposit_unnamed.general", "region": "P1-R03", "quote": "가입기간 12개월"}]}
        return out
    rep, out = run_fake(tmp_path, DEPOSIT, Fake(observe=observe, label=EMPTY_LABELS))
    p = load(out / "files" / "a01" / "result.json")["products"][0]
    assert rep["checks"]["passed"] and p["status"] == "classified" and p["name"] is None
    assert p["templates"] == [{"template_id": "예금성상품-상품명 미노출", "role": "product", "label_schema": "deposit-unnamed"}]
    h = load(out / "handoff" / "s1.json")
    assert h["products"][0]["templates"][0]["labels"] == ["회사명", "예금자보호", "유의사항", "심의번호"]


MARKS = [{"sub": "m1", "fid": "k01", "name": "k.png", "kind": "image", "pages": [
    [R("NH튼튼적금"), R("예금보험공사 보호금융상품 1인당 최고 1억원"), R("보안센터 | 보호금융상품등록부"),
     R("이 예금은 예금자보호법에 따라 보호됩니다. 예금보험공사")]]}]


def test_deposit_marks_checks_logo_candidates_and_keeps_text_in_trace(tmp_path):
    asked = []

    def marks(meta, schema, text):
        asked.append(meta["region_id"])
        return {"protected": "YES" if meta["region_id"].endswith("L001") else "NO", "non_protected": "NO",
                "mark_text": "예금보험공사 보호금융상품 1인당 최고 1억원"}
    rep, out = run_fake(tmp_path, MARKS, Fake(observe=observe_one, label=EMPTY_LABELS, marks=marks))
    d = load(out / "files" / "k01" / "result.json")
    m = d["deposit_insurance_marks"]
    assert rep["checks"]["passed"] and m["version"] == "deposit-marks/1"
    assert m["protected"] == {"status": "PRESENT", "code": None, "region_ids": ["k01_p01-v0-L001"]}
    assert m["non_protected"] == {"status": "ABSENT", "code": None, "region_ids": []}
    # 메뉴 이름('보호금융상품등록부')만 있는 영역은 후보가 아니고, 글자가 짧은(로고에 가까운) 영역부터 확인한다
    assert sorted(asked) == ["k01_p01-v0-L001", "k01_p01-v0-L003"] and "mark_text" not in json.dumps(d, ensure_ascii=False)
    trace = load(out / "trace" / "semantic" / "m1.json")["deposit_marks"]
    assert trace["candidates"][0]["region_id"] == "k01_p01-v0-L001" and trace["checked"][0]["mark_text"]
    h = load(out / "handoff" / "m1.json")
    assert h["deposit_insurance_marks"] == m


def test_deposit_marks_status_rules():
    from layout_pipeline import deposit_marks as M
    c = {"region_id": "r1", "page_id": "p", "bbox": [0, 0, 1, 1]}
    assert M.summarize([c | {"error": "x"}], 0, True)["protected"]["code"] == "CHECK_FAILED"
    assert M.summarize([c | {"protected": "UNSURE", "non_protected": "NO"}], 0, True)["protected"]["code"] == "MARK_UNCLEAR"
    assert M.summarize([c | {"protected": "NO", "non_protected": "NO"}], 3, True)["protected"]["code"] == "TOO_MANY_CANDIDATES"
    assert M.summarize([], 0, False)["non_protected"] == {"status": "UNKNOWN", "code": "PAGES_INCOMPLETE", "region_ids": []}
    assert M.summarize([], 0, True)["protected"]["status"] == "ABSENT"
    assert M.not_checked("DISABLED")["protected"] == {"status": "UNKNOWN", "code": "DISABLED", "region_ids": []}
    img = Image.new("RGB", (100, 40), "white")
    assert M.crop(img, [10, 10, 30, 20]).size == (168, 128)          # 여백 12px 포함 42×32 → 짧은 변 320 을 향해 최대 4배
    assert M.crop(img, [10, 10, 10, 10]) is None
