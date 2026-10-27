"""layout-result/2 검사(docs/12): JSON Schema + 스키마로 표현하기 어려운 참조 규칙.

참조 규칙
- 영역 product_assignment.product_ids 와 labeling.items[].product_id(null 제외)는 같은 문서 products[] 에 있어야 한다.
- labeling.items[].product_id 가 있으면 그 영역의 product_ids 에 있어야 한다(상품별 라벨은 그 영역이 참조한 상품에만).
- status=completed 이고 scope=specific 이면 product_ids 1개 이상, scope=shared 이면 1개 이상(공통 적용 상품 목록).
  scope=unknown 은 미확정이며 product_ids 가 비어도 된다. not_run 은 scope=null·빈 배열(스키마).
- 같은 제출 묶음의 여러 파일에서 같은 product_id 는 같은 상품이다: name 이 서로 다르면(둘 다 null 이 아닐 때) 오류.
- region_id·ocr_id 는 문서 안에서 유일, 영역·미배정 OCR 의 page_id 는 pages[] 에 있어야 한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "layout-result-2.schema.json"
_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        _VALIDATOR = jsonschema.Draft202012Validator(schema)
    return _VALIDATOR


def check_document(doc: dict) -> list[str]:
    errs = [f"schema {'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in _validator().iter_errors(doc)]
    if errs:
        return errs
    pages = {p["page_id"] for p in doc["pages"]}
    products = {p["product_id"] for p in doc["products"]}
    rids = [r["region_id"] for r in doc["regions"]]
    oids = [u["ocr_id"] for u in doc["unassigned_texts"]]
    if len(set(rids)) != len(rids):
        errs.append("region_id 중복")
    if len(set(oids)) != len(oids):
        errs.append("unassigned ocr_id 중복")
    if len({p["product_id"] for p in doc["products"]}) != len(doc["products"]):
        errs.append("products product_id 중복")
    for x in doc["regions"] + doc["unassigned_texts"]:
        if x["page_id"] not in pages:
            errs.append(f"{x.get('region_id') or x.get('ocr_id')}: pages 에 없는 page_id {x['page_id']}")
    for r in doc["regions"]:
        pa, lb = r["product_assignment"], r["labeling"]
        for pid in pa["product_ids"]:
            if pid not in products:
                errs.append(f"{r['region_id']}: products 에 없는 product_id {pid}")
        if pa["status"] == "completed" and pa["scope"] in ("specific", "shared") and not pa["product_ids"]:
            errs.append(f"{r['region_id']}: completed·{pa['scope']} 인데 product_ids 비어 있음")
        if pa["status"] in ("completed", "partial") and pa["scope"] is None:
            errs.append(f"{r['region_id']}: {pa['status']} 인데 scope 없음")
        for it in lb["items"]:
            pid = it["product_id"]
            if pid is not None and pid not in products:
                errs.append(f"{r['region_id']}: 라벨이 products 에 없는 product_id {pid} 참조")
            if pid is not None and pid not in pa["product_ids"]:
                errs.append(f"{r['region_id']}: 라벨 product_id {pid} 가 영역 product_ids 에 없음")
    return errs


def check_submission(docs: list[dict]) -> list[str]:
    """같은 제출 묶음 문서들: 입력 순서·file_id 유일, 같은 product_id 의 이름 일관성."""
    errs = []
    by_sub: dict[str, list[dict]] = {}
    for d in docs:
        by_sub.setdefault(d["submission_id"], []).append(d)
    for sub, ds in by_sub.items():
        fids = [d["source"]["file_id"] for d in ds]
        orders = sorted(d["source"]["input_order"] for d in ds)
        if len(set(fids)) != len(fids):
            errs.append(f"{sub}: file_id 중복")
        if orders != list(range(1, len(ds) + 1)):
            errs.append(f"{sub}: input_order 가 1..{len(ds)} 이 아님 {orders}")
        names: dict[str, set] = {}
        for d in ds:
            for p in d["products"]:
                if p["name"] is not None:
                    names.setdefault(p["product_id"], set()).add(p["name"])
        for pid, ns in names.items():
            if len(ns) > 1:
                errs.append(f"{sub}: 같은 product_id {pid} 에 서로 다른 이름 {sorted(ns)}")
    return errs
