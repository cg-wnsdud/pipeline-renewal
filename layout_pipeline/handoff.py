"""심의 단계 입력(review-input/2): 제출 묶음 하나의 파일별 layout-result/3 에서 심의에 필요한 값만 추린 JSON 하나.

- 묶음(submission) 하나 = JSON 하나. 여러 파일을 한 번에 올린 광고도 한 문서에 파일 → 페이지 → 영역 순으로 담는다.
- 영역은 페이지 안에 읽기 순서로 둔다. 값은 layout-result/3 그대로 옮기고 새로 판단하지 않는다(region_id·bbox·text·표 셀).
- 표는 셀 구조(table-cells/1: n_rows·n_cols·cells[row, col, rowspan, colspan, text])로 넘긴다. 셀 구조를 얻지 못한 표는
  is_table=true, table=null 이고 needs_review 로 표시한다.
- 상품 소속은 scope(specific·shared·mixed·unknown)와 product_ids, 라벨은 상품별 label_ids 로 둔다. 상품의 템플릿마다 쓸 수 있는
  라벨 목록(labels)을 함께 싣는다.
- 검토 사유는 판독 품질(quality_reasons: review.quality_reasons·판독 실패·셀 구조 없음)과 의미 단계(semantic_codes)를 나눠 둔다.
  판정이 '판독 문구를 믿을 수 있는가'에는 quality_reasons 만 쓴다(review-input/1 은 둘을 합쳐 의미 단계 사유만 있는 영역도
  판독 불가처럼 보였다, docs/21). needs_review 는 둘 중 하나라도 있으면 true 다. 경계·보충 같은 맥락 사유는 넣지 않는다.
- review-input/2(docs/21)에서 더한 것: 등록 템플릿 요청(template_request), 예금자보호 표시(deposit_insurance_marks), 묶음 안
  쪽 연번(pages[].seq), 문구 출처(text_source: hwp | vlm), HWP 검토 HTML 위치(files[].hwp_review_html).
- 넣지 않는 것: 실행·버전 정보, 페이지 경로·경계·진단, 미배정 OCR, 판단 근거(trace). 필요하면 source 의 result_id 로 원래 결과를 찾는다.
- 경로(이미지·HWP 검토 HTML)는 이 JSON 파일 위치 기준 상대 경로다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema

from . import deposit_marks
from . import semantic_assets as A
from .jsonio import dump, load
from .read_stage import reading_order

CONTRACT = "review-input/2"
PROCESSED = ("READY", "CANDIDATE_RUN")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "review-input-2.schema.json"
HWP_REGION_MARK = "-h0-H"          # HWP 구조 영역 ID(hwp_structure: <page_id>-h0-H000). 문구가 HWP 원문이다
_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        _VALIDATOR = jsonschema.Draft202012Validator(schema)
    return _VALIDATOR


def _rel(target: str, start: Path) -> str:
    return os.path.relpath(os.path.abspath(target), os.path.abspath(start)).replace("\\", "/")


def _labels_of(template: dict) -> list[str]:
    return A.label_schema(template["label_schema"])["labels"] if template.get("label_schema") else []


def _product(p: dict) -> dict:
    return {"product_id": p["product_id"], "name": p["name"], "product_group": p["product_group"], "status": p["status"],
            "templates": [t | {"labels": _labels_of(t)} for t in p["templates"]], "pension": p["pension"],
            "files": p["files"], "pages": p["pages"]}


def _table(r: dict) -> tuple[bool | None, dict | None]:
    t = r["table"]
    if t is None:
        return False, None
    if t["status"] != "table":
        return (False if t["status"] == "not_table" else None), None
    if not t.get("cells"):
        return True, None
    return True, {"n_rows": t["n_rows"], "n_cols": t["n_cols"], "cells": [dict(c) for c in t["cells"]]}


def _text_source(r: dict) -> str | None:
    if r["text"] is None:
        return None
    return "hwp" if HWP_REGION_MARK in r["region_id"] else "vlm"


def _region(r: dict) -> dict:
    is_table, table = _table(r)
    a = r["product_assignment"]
    quality = list(r["review"]["quality_reasons"])
    if r["text_status"] == "read_failed" and "read_failed" not in quality:
        quality.append("read_failed")
    if is_table and table is None and not {"table_structure_missing", "table_structure_failed"} & set(quality):
        quality.append("table_structure_missing")
    semantic = list(dict.fromkeys(r["semantic_review"]["codes"]))
    return {"region_id": r["region_id"], "bbox": r["bbox"], "text": r["text"], "text_status": r["text_status"],
            "text_source": _text_source(r), "is_table": is_table, "table": table,
            "product": {"scope": a["scope"], "product_ids": list(a["product_ids"])},
            "labels": [{"product_id": i["product_id"], "label_ids": list(i["label_ids"])} for i in r["labeling"]["items"]],
            "quality_reasons": quality, "semantic_codes": semantic, "needs_review": bool(quality or semantic)}


def hwp_review_html(doc: dict, doc_path: str) -> str | None:
    """HWP·HWPX 파일의 검토 HTML(기존 변환기가 만든 원문 재구성 화면) 절대 경로. 변환 PDF 와 같은 폴더의
    <file_id>-review-html/*.review.html(hwp_bridge). 없거나 HWP 가 아니면 None."""
    if doc["source"]["file_type"] not in ("hwp", "hwpx") or not doc["pages"]:
        return None
    page_png = os.path.normpath(os.path.join(os.path.dirname(doc_path), doc["pages"][0]["image"]["path"]))
    folder = Path(page_png).parent / "hwp" / f"{doc['source']['file_id']}-review-html"
    found = sorted(folder.glob("*.review.html")) if folder.is_dir() else []
    return str(found[0]) if found else None


def build(docs: list[dict], paths: dict[str, str], out_path: Path) -> dict:
    """묶음 하나의 layout-result/3 문서들(입력 순서) → review-input/2. paths: file_id → result.json 경로(상대 경로 계산용)."""
    docs = sorted(docs, key=lambda d: d["source"]["input_order"])
    subs = {d["submission_id"] for d in docs}
    if len(subs) != 1:
        raise ValueError(f"한 묶음의 문서만 받는다: {sorted(subs)}")
    products: dict[str, dict] = {}
    pages, files = [], []
    for d in docs:
        for p in d["products"]:
            products.setdefault(p["product_id"], _product(p))
        path = paths[d["source"]["file_id"]]
        fdir = os.path.dirname(path)
        html = hwp_review_html(d, path)
        files.append({k: d["source"][k] for k in ("file_id", "file_name", "file_type", "input_order", "page_count")}
                     | {"hwp_review_html": _rel(html, out_path.parent) if html else None})
        for pg in d["pages"]:
            regs = reading_order([r for r in d["regions"] if r["page_id"] == pg["page_id"]])
            pages.append({"page_id": pg["page_id"], "file_id": d["source"]["file_id"], "page_no": pg["page_no"],
                          "seq": len(pages) + 1, "width": pg["width"], "height": pg["height"],
                          "image": {"path": _rel(os.path.join(fdir, pg["image"]["path"]), out_path.parent),
                                    "sha256": pg["image"]["sha256"]},
                          "processed": pg["execution_status"] in PROCESSED,
                          "regions": [_region(r) for r in regs]})
    first = docs[0]
    req = first["semantic"].get("template_request") or {"template_ids": [], "mode": "auto"}
    return {"contract": CONTRACT, "submission_id": first["submission_id"],
            "template_request": {"template_ids": list(req["template_ids"]), "mode": req["mode"]},
            "files": files,
            "products": [products[k] for k in sorted(products)],
            # 이 기능 이전의 layout-result/3 이면 확인하지 않은 것(NOT_CHECKED)
            "deposit_insurance_marks": first.get("deposit_insurance_marks") or deposit_marks.not_checked("NOT_CHECKED"),
            "pages": pages,
            "source": {d["source"]["file_id"]: d["result_id"] for d in docs}}


def check(doc: dict, docs: list[dict]) -> list[str]:
    """스키마 + 참조와 값 보존 검사: 모든 영역이 한 번씩 있고 bbox·text 가 원래 결과와 같으며, 영역이 가리키는 상품·라벨이 정의에 있다."""
    problems = [f"schema {'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in _validator().iter_errors(doc)]
    if problems:
        return problems
    src = {r["region_id"]: r for d in docs for r in d["regions"]}
    seen = [r["region_id"] for p in doc["pages"] for r in p["regions"]]
    if sorted(seen) != sorted(src):
        problems.append(f"영역 집합이 다름: 빠짐 {sorted(set(src) - set(seen))[:5]} 추가 {sorted(set(seen) - set(src))[:5]}")
    if [p["seq"] for p in doc["pages"]] != list(range(1, len(doc["pages"]) + 1)):
        problems.append("pages seq 가 1..n 이 아님")
    products = {p["product_id"]: p for p in doc["products"]}
    for p in doc["pages"]:
        for r in p["regions"]:
            o = src.get(r["region_id"])
            if o and (o["bbox"] != r["bbox"] or o["text"] != r["text"] or o["page_id"] != p["page_id"]):
                problems.append(f"{r['region_id']}: bbox·text·페이지가 원래 결과와 다름")
            for pid in r["product"]["product_ids"] + [i["product_id"] for i in r["labels"]]:
                if pid not in products:
                    problems.append(f"{r['region_id']}: 정의에 없는 상품 {pid}")
            for i in r["labels"]:
                allowed = {x for t in products.get(i["product_id"], {}).get("templates", []) for x in t["labels"]}
                if set(i["label_ids"]) - allowed:
                    problems.append(f"{r['region_id']}: 템플릿 라벨에 없는 값 {sorted(set(i['label_ids']) - allowed)}")
            if r["is_table"] and r["table"]:
                n = r["table"]
                if any(c["row"] + c["rowspan"] > n["n_rows"] or c["col"] + c["colspan"] > n["n_cols"] for c in n["cells"]):
                    problems.append(f"{r['region_id']}: 표 셀이 격자 밖")
    marks = doc.get("deposit_insurance_marks") or {}
    for kind in ("protected", "non_protected"):
        if kind in marks and set(marks[kind]["region_ids"]) - set(seen):
            problems.append(f"deposit_insurance_marks.{kind}: 묶음에 없는 영역")
    return problems


def write_all(sem_dir: Path) -> dict:
    """의미 단계 출력 폴더(<sem>/files/index.json)의 모든 묶음에 대해 <sem>/handoff/<submission_id>.json 을 쓴다."""
    idx = load(sem_dir / "files" / "index.json")["files"]
    by_sub: dict[str, list[tuple[dict, str]]] = {}
    for f in idx:
        path = str(sem_dir / f["result"])
        by_sub.setdefault(f["submission_id"], []).append((load(path), path))
    out = {}
    for sub, items in by_sub.items():
        docs = [d for d, _ in items]
        target = sem_dir / "handoff" / f"{sub}.json"
        doc = build(docs, {d["source"]["file_id"]: p for d, p in items}, target)
        problems = check(doc, docs)
        dump(target, doc)
        out[sub] = {"path": str(target), "problems": problems}
    dump(sem_dir / "handoff" / "check.json", {"contract": CONTRACT, "passed": not any(v["problems"] for v in out.values()),
                                               "submissions": out})
    return out
