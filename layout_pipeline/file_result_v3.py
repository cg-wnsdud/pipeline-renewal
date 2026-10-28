"""입력 파일별 의미 단계 결과(layout-result/3, docs/14·16): 파싱 snapshot(layout-result/2) + 상품·템플릿·영역 배정·라벨.

- 파싱 값(페이지·영역 ID·bbox·최종 text·text_status·표 결과·파싱 검토·후보 경계·미배정 OCR)은 layout-result/2 에서 그대로
  옮긴다. 바뀌는 것은 영역의 product_assignment·labeling 값, 새 필드 semantic_review, 최상위 products·parse·semantic 이다.
  페이지 이미지 경로만 새 파일 위치 기준 상대 경로로 다시 쓴다(같은 이미지를 가리킨다).
- 상품군·템플릿은 products[] 에만 두고 영역에는 product_id 만 둔다. 같은 묶음의 여러 파일은 같은 상품 정의를 그대로 담는다.
  products[].pages 는 상품이 걸친 페이지(다른 파일 포함), files 는 그 페이지들의 파일이다.
- product_assignment.basis: model(관측 응답으로 배정) | null(미실행·실패·상품 없음). single_product 는 이전 inventory-first 결과 호환용.
  semantic.classification_mode: observe(고정 경로) | legacy(이전 실행 재구성).
- 모델 프롬프트·원시 응답·근거 인용·특징 ID·설명문은 넣지 않는다(trace/semantic). 상태는 짧은 코드로만 남긴다.
- result_id 는 파싱 result_id + 의미 단계 프로필·자산 버전 + 그 묶음의 의미 단계 응답 해시로 정한다. 저장 응답으로 다시
  만들면 같은 값이 나온다.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from . import semantic_assets as A
from .jsonio import dump

CONTRACT = "layout-result/3"
SEMANTIC_PROFILE = "semantic-default/1"
V2_REGION_KEYS = ("region_id", "page_id", "bbox", "table_candidate", "text", "text_status", "table", "review", "candidate_boundary")


def _rel(target: str | Path, start: str | Path) -> str:
    return os.path.relpath(os.path.abspath(target), os.path.abspath(start)).replace("\\", "/")


def public_product(p: dict) -> dict:
    """결과 JSON 의 상품 정의(같은 묶음의 모든 파일에 같은 값)."""
    templates = [{"template_id": A.template(c)["template_id"], "role": "product", "label_schema": A.template(c)["label_schema"]}
                 for c in p["codes"]]
    if p["pension"]:
        comp = A.component_templates()
        templates += [{"template_id": comp[c], "role": "component", "label_schema": None} for c in p["pension"]["components"]]
    return {"product_id": p["product_id"], "name": p["name"], "product_group": p["product_group"], "status": p["status"],
            "templates": templates, "pension": p["pension"], "files": p["files"], "pages": list(p["pages"]),
            "review": {"required": bool(p["review"]), "codes": list(p["review"])}}


def result_id(parse_id: str, versions: dict, call_hashes: list, profile: str = SEMANTIC_PROFILE) -> str:
    blob = json.dumps({"parse": parse_id, "profile": profile, "versions": versions, "calls": sorted(call_hashes)},
                      ensure_ascii=False, sort_keys=True)
    return "lr3-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def build_docs(out_dir: Path, docs_v2: list[dict], paths_v2: dict[str, str], products: list[dict], assignments: dict,
               labels: dict, status: str, versions: dict, call_hashes: list, trace_path: Path,
               profile: str = SEMANTIC_PROFILE, classification_mode: str = "legacy") -> list[dict]:
    """묶음 하나의 파일별 layout-result/3 문서(입력 순서)."""
    pub = {p["product_id"]: public_product(p) for p in products}
    docs = []
    for d in sorted(docs_v2, key=lambda d: d["source"]["input_order"]):
        fid = d["source"]["file_id"]
        fdir = out_dir / "files" / fid
        v2dir = os.path.dirname(paths_v2[fid])
        pages = [p | {"image": p["image"] | {"path": _rel(os.path.join(v2dir, p["image"]["path"]), fdir)}} for p in d["pages"]]
        regions, used = [], []
        for r in d["regions"]:
            a, lb = assignments[r["region_id"]], labels[r["region_id"]]
            codes = list(dict.fromkeys(a["codes"] + lb["codes"]))
            regions.append({k: r[k] for k in V2_REGION_KEYS} | {
                "product_assignment": {"status": a["status"], "scope": a["scope"], "product_ids": list(a["product_ids"]),
                                       "basis": a["basis"]},
                "labeling": {"status": lb["status"], "items": [dict(i) for i in lb["items"]]},
                "semantic_review": {"required": bool(codes), "codes": codes}})
            used += a["product_ids"]
        prods = [pub[p["product_id"]] for p in products if p["product_id"] in used or fid in p["files"]]
        docs.append({
            "contract": CONTRACT, "result_id": result_id(d["result_id"], versions, call_hashes, profile),
            "submission_id": d["submission_id"], "source": d["source"],
            "parse": {"contract": d["contract"], "result_id": d["result_id"], "profile": d["parser_profile"]["name"],
                      "path": _rel(paths_v2[fid], fdir)},
            "semantic": {"profile": profile, "classification_mode": classification_mode, "status": status, "versions": versions},
            "pages": pages, "products": prods, "regions": regions, "unassigned_texts": d["unassigned_texts"],
            "trace_ref": {"parse_result": _rel(paths_v2[fid], fdir), "semantic_trace": _rel(trace_path, fdir)}})
    return docs


def write_docs(out_dir: Path, docs: list[dict]) -> list[dict]:
    """files/<file_id>/result.json 과 목록 항목. 같은 file_id 가 두 번 나오면 오류."""
    index, seen = [], set()
    for d in docs:
        fid = d["source"]["file_id"]
        if fid in seen:
            raise ValueError(f"중복 file_id {fid}")
        seen.add(fid)
        dump(out_dir / "files" / fid / "result.json", d)
        rs = d["regions"]
        index.append({"submission_id": d["submission_id"], "input_order": d["source"]["input_order"], "file_id": fid,
                      "file_name": d["source"]["file_name"], "pages": len(d["pages"]), "regions": len(rs),
                      "result_id": d["result_id"], "parse_result_id": d["parse"]["result_id"], "result": f"files/{fid}/result.json",
                      "semantic_status": d["semantic"]["status"],
                      "products": [{"product_id": p["product_id"], "name": p["name"], "status": p["status"],
                                    "templates": [t["template_id"] for t in p["templates"]]} for p in d["products"]],
                      "assignment": dict(Counter(f"{r['product_assignment']['status']}:{r['product_assignment']['scope']}" for r in rs)),
                      "assignment_basis": dict(Counter(str(r["product_assignment"]["basis"]) for r in rs)),
                      "labeling": dict(Counter(r["labeling"]["status"] for r in rs)),
                      "semantic_review": sum(r["semantic_review"]["required"] for r in rs),
                      "parse_review": sum(r["review"]["required"] for r in rs)})
    return index
