"""layout-result/3 검사(docs/14): JSON Schema + 참조·상태 규칙 + 묶음 일관성 + 파싱 snapshot 보존.

참조·상태 규칙(스키마로 표현하기 어려운 것)
- 영역 product_ids·라벨 product_id 는 같은 문서 products[] 에 있어야 하고, 라벨 product_id 는 그 영역 product_ids 안에 있어야 한다.
- 배정 completed: specific 은 상품 1개, mixed 는 2개 이상. 라벨 completed 는 배정 상품마다 item 1개, partial 은 일부.
  라벨이 있으면 배정이 completed 여야 한다.
- label_ids 는 그 상품 템플릿(role=product)의 라벨 스키마 허용 구분값 안에 있어야 한다.
- 퇴직연금 기본 템플릿이 있는 상품만 pension 값이 있고, components 와 운용상품 템플릿이 같아야 한다.
- semantic_review.required 는 codes 가 있을 때만 true.
- 상품 pages: 이 문서가 그 상품 files 에 들면 이 문서 페이지가 하나 이상 있고, 그 상품에 배정된 영역의 페이지는 모두 pages 에 있다.
- 같은 묶음의 여러 파일에서 같은 product_id 의 정의(이름·상품군·상태·템플릿·퇴직연금·파일·페이지·검토)는 같아야 하고,
  pages 는 묶음의 페이지이며 files 는 그 페이지들의 파일과 같다.
- 보존: 페이지·영역(ID·bbox·text·text_status·표·파싱 검토·후보 경계)·미배정 OCR·source 가 근거 layout-result/2 와 같다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema

from . import semantic_assets as A
from .file_result_v3 import V2_REGION_KEYS

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "layout-result-3.schema.json"
_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        _VALIDATOR = jsonschema.Draft202012Validator(schema)
    return _VALIDATOR


def allowed_labels(product: dict) -> set[str]:
    keys = [t["label_schema"] for t in product["templates"] if t["role"] == "product" and t["label_schema"]]
    return {x for k in keys for x in A.label_schema(k)["labels"]}


def check_document(doc: dict) -> list[str]:
    errs = [f"schema {'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in _validator().iter_errors(doc)]
    if errs:
        return errs
    pages = {p["page_id"] for p in doc["pages"]}
    products = {p["product_id"]: p for p in doc["products"]}
    if len(products) != len(doc["products"]):
        errs.append("products product_id 중복")
    for ids, what in (([r["region_id"] for r in doc["regions"]], "region_id"), ([u["ocr_id"] for u in doc["unassigned_texts"]], "ocr_id")):
        if len(set(ids)) != len(ids):
            errs.append(f"{what} 중복")
    for x in doc["regions"] + doc["unassigned_texts"]:
        if x["page_id"] not in pages:
            errs.append(f"{x.get('region_id') or x.get('ocr_id')}: pages 에 없는 page_id")
    pension_base = A.template("pension")["template_id"]
    comp = A.component_templates()
    for p in doc["products"]:
        if doc["source"]["file_id"] not in p["files"]:
            errs.append(f"{p['product_id']}: files 에 이 문서 file_id 없음")
        elif not set(p["pages"]) & pages:
            errs.append(f"{p['product_id']}: pages 에 이 문서 페이지가 없음")
        base = any(t["template_id"] == pension_base for t in p["templates"])
        if base != (p["pension"] is not None):
            errs.append(f"{p['product_id']}: 퇴직연금 기본 템플릿과 pension 값이 맞지 않음")
        comps = [t["template_id"] for t in p["templates"] if t["role"] == "component"]
        if p["pension"] and comps != [comp[c] for c in p["pension"]["components"]]:
            errs.append(f"{p['product_id']}: 운용상품 템플릿과 components 가 다름")
        if comps and not base:
            errs.append(f"{p['product_id']}: 퇴직연금 기본 템플릿 없이 운용상품 템플릿")
        if p["review"]["required"] != bool(p["review"]["codes"]):
            errs.append(f"{p['product_id']}: review.required 와 codes 불일치")
    for r in doc["regions"]:
        rid, pa, lb = r["region_id"], r["product_assignment"], r["labeling"]
        for pid in pa["product_ids"]:
            if pid not in products:
                errs.append(f"{rid}: products 에 없는 product_id {pid}")
            elif r["page_id"] not in products[pid]["pages"]:
                errs.append(f"{rid}: {pid} pages 에 이 영역 페이지가 없음")
        n = len(pa["product_ids"])
        if pa["status"] == "completed" and ((pa["scope"] == "specific" and n != 1) or (pa["scope"] == "mixed" and n < 2)):
            errs.append(f"{rid}: {pa['scope']} 인데 상품 {n}개")
        if lb["items"] and pa["status"] != "completed":
            errs.append(f"{rid}: 배정이 완료되지 않았는데 라벨이 있음")
        seen = []
        for it in lb["items"]:
            pid = it["product_id"]
            if pid not in pa["product_ids"]:
                errs.append(f"{rid}: 라벨 product_id {pid} 가 영역 product_ids 에 없음")
                continue
            if pid in seen:
                errs.append(f"{rid}: 같은 상품 라벨 item 중복 {pid}")
            seen.append(pid)
            if pid in products:
                bad = set(it["label_ids"]) - allowed_labels(products[pid])
                if bad:
                    errs.append(f"{rid}: {pid} 허용 라벨 밖 {sorted(bad)}")
        if lb["status"] == "completed" and sorted(seen) != sorted(pa["product_ids"]):
            errs.append(f"{rid}: 라벨 completed 인데 배정 상품마다 item 이 있지 않음")
        if lb["status"] == "partial" and len(seen) >= n:
            errs.append(f"{rid}: 라벨 partial 인데 모든 상품에 item 이 있음")
        if r["semantic_review"]["required"] != bool(r["semantic_review"]["codes"]):
            errs.append(f"{rid}: semantic_review.required 와 codes 불일치")
    return errs


def check_submission(docs: list[dict]) -> list[str]:
    """같은 묶음: file_id 유일·입력 순서 1..n, 같은 product_id 의 정의 동일, 상품 files 가 실제로 그 상품을 담은 파일과 같음."""
    errs = []
    by_sub: dict[str, list[dict]] = {}
    for d in docs:
        by_sub.setdefault(d["submission_id"], []).append(d)
    for sub, ds in by_sub.items():
        fids = [d["source"]["file_id"] for d in ds]
        if len(set(fids)) != len(fids):
            errs.append(f"{sub}: file_id 중복")
        if sorted(d["source"]["input_order"] for d in ds) != list(range(1, len(ds) + 1)):
            errs.append(f"{sub}: input_order 가 1..{len(ds)} 이 아님")
        defs: dict[str, str] = {}
        holders: dict[str, set] = {}
        page_file = {p["page_id"]: d["source"]["file_id"] for d in ds for p in d["pages"]}
        for d in ds:
            for p in d["products"]:
                blob = json.dumps(p, ensure_ascii=False, sort_keys=True)
                if defs.setdefault(p["product_id"], blob) != blob:
                    errs.append(f"{sub}: {p['product_id']} 정의가 파일마다 다름")
                holders.setdefault(p["product_id"], set()).add(d["source"]["file_id"])
        for pid, blob in defs.items():
            p = json.loads(blob)
            if set(p["files"]) != holders[pid]:
                errs.append(f"{sub}: {pid} files 와 그 상품을 담은 파일이 다름")
            if any(pg not in page_file for pg in p["pages"]):
                errs.append(f"{sub}: {pid} pages 에 묶음에 없는 페이지")
            elif {page_file[pg] for pg in p["pages"]} != set(p["files"]):
                errs.append(f"{sub}: {pid} files 와 pages 의 파일이 다름")
    return errs


def check_preservation(doc3: dict, path3: str | Path, doc2: dict, path2: str | Path) -> list[str]:
    """의미 단계가 파싱 값을 바꾸지 않았는지(이미지 경로는 같은 파일을 가리키는지로 본다)."""
    errs = []
    if doc3["parse"]["result_id"] != doc2["result_id"]:
        errs.append("parse.result_id 가 근거 snapshot 과 다름")
    for k in ("submission_id", "source", "unassigned_texts"):
        if doc3[k] != doc2[k]:
            errs.append(f"{k} 변경")
    d3, d2 = os.path.dirname(path3), os.path.dirname(path2)
    if len(doc3["pages"]) != len(doc2["pages"]):
        errs.append("페이지 수 변경")
    for p3, p2 in zip(doc3["pages"], doc2["pages"]):
        if {k: v for k, v in p3.items() if k != "image"} != {k: v for k, v in p2.items() if k != "image"}:
            errs.append(f"{p2['page_id']}: 페이지 값 변경")
        same = os.path.normcase(os.path.abspath(os.path.join(d3, p3["image"]["path"]))) == \
            os.path.normcase(os.path.abspath(os.path.join(d2, p2["image"]["path"])))
        if p3["image"]["sha256"] != p2["image"]["sha256"] or not same:
            errs.append(f"{p2['page_id']}: 페이지 이미지 참조 변경")
    if [r["region_id"] for r in doc3["regions"]] != [r["region_id"] for r in doc2["regions"]]:
        errs.append("영역 목록·순서 변경")
    for r3, r2 in zip(doc3["regions"], doc2["regions"]):
        for k in V2_REGION_KEYS:
            if r3[k] != r2[k]:
                errs.append(f"{r2['region_id']}: {k} 변경")
    return errs
