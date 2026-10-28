"""심의 단계 입력(review-input/1): 제출 묶음 하나의 파일별 layout-result/3 에서 심의에 필요한 값만 추린 JSON 하나.

- 묶음(submission) 하나 = JSON 하나. 여러 파일을 한 번에 올린 광고도 한 문서에 파일 → 페이지 → 영역 순으로 담는다.
- 영역은 페이지 안에 읽기 순서로 둔다. 값은 layout-result/3 그대로 옮기고 새로 판단하지 않는다(region_id·bbox·text·표 셀).
- 표는 셀 구조(table-cells/1: n_rows·n_cols·cells[row, col, rowspan, colspan, text])로 넘긴다. 셀 구조를 얻지 못한 표는
  is_table=true, table=null 이고 needs_review 로 표시한다.
- 상품 소속은 scope(specific·shared·mixed·unknown)와 product_ids, 라벨은 상품별 label_ids 로 둔다. 상품의 템플릿마다 쓸 수 있는
  라벨 목록(labels)을 함께 싣는다.
- needs_review 는 판독 품질 사유(review.quality_reasons)·판독 실패·의미 단계 검토(semantic_review.codes)로 정한다. 경계·보충 같은
  맥락 사유는 넣지 않는다(layout-result/3 의 review.context_reasons 에 남는다).
- 넣지 않는 것: 실행·버전 정보, 페이지 경로·경계·진단, 미배정 OCR, 판단 근거(trace). 필요하면 source 의 result_id 로 원래 결과를 찾는다.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import semantic_assets as A
from .jsonio import dump, load
from .read_stage import reading_order

CONTRACT = "review-input/1"
PROCESSED = ("READY", "CANDIDATE_RUN")


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


def _region(r: dict) -> dict:
    is_table, table = _table(r)
    a = r["product_assignment"]
    reasons = list(dict.fromkeys(r["review"]["quality_reasons"] + r["semantic_review"]["codes"]))
    if r["text_status"] == "read_failed" and "read_failed" not in reasons:
        reasons.append("read_failed")
    if is_table and table is None and not {"table_structure_missing", "table_structure_failed"} & set(reasons):
        reasons.append("table_structure_missing")
    return {"region_id": r["region_id"], "bbox": r["bbox"], "text": r["text"], "text_status": r["text_status"],
            "is_table": is_table, "table": table,
            "product": {"scope": a["scope"], "product_ids": list(a["product_ids"])},
            "labels": [{"product_id": i["product_id"], "label_ids": list(i["label_ids"])} for i in r["labeling"]["items"]],
            "needs_review": bool(reasons), "review_reasons": reasons}


def build(docs: list[dict], paths: dict[str, str], out_path: Path) -> dict:
    """묶음 하나의 layout-result/3 문서들(입력 순서) → review-input/1. paths: file_id → result.json 경로(이미지 상대 경로 계산용)."""
    docs = sorted(docs, key=lambda d: d["source"]["input_order"])
    subs = {d["submission_id"] for d in docs}
    if len(subs) != 1:
        raise ValueError(f"한 묶음의 문서만 받는다: {sorted(subs)}")
    products: dict[str, dict] = {}
    pages = []
    for d in docs:
        for p in d["products"]:
            products.setdefault(p["product_id"], _product(p))
        fdir = os.path.dirname(paths[d["source"]["file_id"]])
        for pg in d["pages"]:
            regs = reading_order([r for r in d["regions"] if r["page_id"] == pg["page_id"]])
            pages.append({"page_id": pg["page_id"], "file_id": d["source"]["file_id"], "page_no": pg["page_no"],
                          "width": pg["width"], "height": pg["height"],
                          "image": {"path": _rel(os.path.join(fdir, pg["image"]["path"]), out_path.parent),
                                    "sha256": pg["image"]["sha256"]},
                          "processed": pg["execution_status"] in PROCESSED,
                          "regions": [_region(r) for r in regs]})
    return {"contract": CONTRACT, "submission_id": docs[0]["submission_id"],
            "files": [{k: d["source"][k] for k in ("file_id", "file_name", "file_type", "input_order", "page_count")} for d in docs],
            "products": [products[k] for k in sorted(products)],
            "pages": pages,
            "source": {d["source"]["file_id"]: d["result_id"] for d in docs}}


def check(doc: dict, docs: list[dict]) -> list[str]:
    """참조와 값 보존 검사: 모든 영역이 한 번씩 있고 bbox·text 가 원래 결과와 같으며, 영역이 가리키는 상품·라벨이 정의에 있다."""
    problems = []
    src = {r["region_id"]: r for d in docs for r in d["regions"]}
    seen = [r["region_id"] for p in doc["pages"] for r in p["regions"]]
    if sorted(seen) != sorted(src):
        problems.append(f"영역 집합이 다름: 빠짐 {sorted(set(src) - set(seen))[:5]} 추가 {sorted(set(seen) - set(src))[:5]}")
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
