"""입력 파일별 최종 결과(layout-result/2, docs/12). 라벨링 전 단계의 파싱 snapshot.

- 입력 파일 하나 → `<out>/files/<file_id>/result.json` 하나(여러 페이지 PDF/HWP 는 모든 페이지를 한 파일에).
- 문구·좌표·상태·표 결과는 layout-result/1(실행 단위 결과)의 값을 그대로 옮긴다. 바꾸거나 합치지 않는다.
- 영역마다 상품 배정(product_assignment)·라벨링(labeling) 필드를 두고 이번 단계에서는 status=not_run 으로 둔다.
  not_run 의 빈 배열은 '상품·라벨 없음' 판정이 아니다. 최상위 products 는 빈 배열이다.
- 프롬프트·중간 후보·원시 응답·임계값·평가·정답은 넣지 않는다(trace·평가 폴더). 정답 평가 이슈(GOLD_*)도 넣지 않는다.
- result_id 는 파일 해시 + 파서 프로필 + 사용한 OCR·VLM 원시 응답 해시로 정한다. 저장 응답으로 재구성하면 같은 값이 나온다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .jsonio import dump, load

CONTRACT = "layout-result/2"
PROFILE_NAME = "page-first-default/1"
NOT_RUN_ASSIGNMENT = {"status": "not_run", "scope": None, "product_ids": []}
NOT_RUN_LABELING = {"status": "not_run", "items": []}
EVAL_ISSUE_PREFIX = "GOLD_"          # Stage 1 의 정답 평가 진단(평가 전용) — 최종 JSON 에서 제외


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve(root: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else root / q


def parser_profile(settings: dict, stage1_run: dict) -> dict:
    """실제로 쓴 판독 방식·정책 버전(실행 설정에서 읽음, 프롬프트·임계값 원문은 trace)."""
    read_mode = settings.get("read_mode") or "crop"
    page_first = read_mode != "crop"
    rule = settings.get("assignment_rule") or stage1_run.get("assignment_rule") or {}
    pol = stage1_run.get("routing_policy") or {}
    table_mode = settings.get("table_read") if settings.get("table_read") not in (None, "legacy") else settings.get("table_request")
    default = page_first and settings.get("verify_request") == "region-reread/1" and table_mode == "table-cells/1" \
        and rule.get("core_shrink") == 0
    return {
        "name": PROFILE_NAME if default else "custom",
        "read_mode": read_mode,
        "verify_mode": settings.get("verify_request") if page_first else None,
        "table_mode": table_mode,
        "assignment_rule": {"name": rule.get("name"), "core_shrink": rule.get("core_shrink")},
        "versions": {
            "page_request": settings.get("page_request"),
            "general_request": settings.get("general_request"),
            "page_band": "page-band/1" if page_first else None,
            "reading_order_check": (settings.get("verify_conditions") or {}).get("reading_order"),
            "routing_policy": {"long_aspect_min": pol.get("long_aspect_min"), "status": pol.get("status")},
            "boundary_policy": stage1_run.get("boundary_policy") or ("section-pack/2" if stage1_run.get("auto_boundary_pages") else None),
            "run_candidate_boundaries": bool(stage1_run.get("run_candidate_boundaries")),
            "layout_options": stage1_run.get("layout_options"),
            "pdf_dpi": stage1_run.get("pdf_dpi"),
            "vlm_temperature": settings.get("temperature"),
            "unassigned_output": (settings.get("unassigned_output") or {}).get("name"),
        },
    }


def _ocr_hashes(root: Path, calls: list[dict], page_ids: set[str]) -> list[list[str]]:
    out = []
    for c in calls:
        if c["page_id"] in page_ids and c.get("ok") and c.get("view_kind") != "scout":
            raw = _resolve(root, c["raw_path"])
            out.append([c["call_id"], _sha(raw.read_bytes()) if raw.is_file() else c.get("sent_sha256") or ""])
    return sorted(out)


def _vlm_hashes(trace_dir: Path, region_ids: set[str]) -> list[list[str]]:
    out = []
    for path in sorted((trace_dir / "vlm").glob("*.json")):
        rec = load(path)
        ids = set(rec["request_meta"].get("region_ids") or [])
        if ids & region_ids:
            out.append([rec["call_id"], _sha(json.dumps([rec.get("content"), rec.get("error")], ensure_ascii=False).encode("utf-8"))])
    return out


def result_id(source_sha: str, profile: dict, ocr: list, vlm: list) -> str:
    blob = json.dumps({"source": source_sha, "profile": profile, "ocr": ocr, "vlm": vlm}, ensure_ascii=False, sort_keys=True)
    return "lr2-" + _sha(blob.encode("utf-8"))[:20]


def _table(t: dict | None) -> dict | None:
    """기존 표 결과를 그대로(값 변경 없음). 표 후보가 아니면 null."""
    return None if t is None else dict(t)


def _region(r: dict) -> dict:
    return {"region_id": r["region_id"], "page_id": r["page_id"], "bbox": r["bbox"],
            "table_candidate": r["table_candidate"], "text": r["text"], "text_status": r["text_status"],
            "table": _table(r["table"]),
            "review": {"required": bool(r["needs_review"]), "quality_reasons": list(r["review"]["quality"]),
                       "context_reasons": list(r["review"]["context"])},
            "candidate_boundary": bool(r.get("candidate_boundary")),
            "product_assignment": dict(NOT_RUN_ASSIGNMENT), "labeling": dict(NOT_RUN_LABELING)}


def _unassigned(u: dict) -> dict:
    return {"ocr_id": u["ocr_id"], "page_id": u["page_id"], "bbox": u["ocr_bbox"], "ocr_text": u["ocr_text"],
            "reason": u["reason"], "candidate_region_ids": list(u["candidate_region_ids"]), "empty_text": u["empty_text"],
            "final": False}


def build_file_results(root: Path, out_dir: Path, result_v1: dict, records: list[dict], stage1_dir: Path,
                       trace_dir: Path) -> list[dict]:
    """실행 단위 결과를 파일별 문서로 나눈다. 반환: 파일별 문서(쓰기 전) 목록, 입력 순서."""
    stage1_run = load(stage1_dir / "run.json")
    calls = load(stage1_dir / "calls.json")
    profile = parser_profile(result_v1["settings"], stage1_run)
    rec_by_page = {r["page"]["page_id"]: r for r in records}
    pages_v1 = {p["page_id"]: p for p in result_v1["pages"]}
    files: dict[str, dict] = {}
    order: dict[str, list[str]] = {}
    for rec in records:                                      # Stage 1 페이지 순서 = 입력 파일 순서 → 페이지 순서
        pg = rec["page"]
        f = files.setdefault(pg["file_id"], {"submission_id": pg["submission_id"], "pages": [], "first": len(files)})
        f["pages"].append(pg["page_id"])
        order.setdefault(pg["submission_id"], [])
        if pg["file_id"] not in order[pg["submission_id"]]:
            order[pg["submission_id"]].append(pg["file_id"])
    docs = []
    for file_id, f in files.items():
        page_ids = f["pages"]
        first = rec_by_page[page_ids[0]]["page"]
        src = first["source"]
        file_dir = out_dir / "files" / file_id
        pages = []
        for pid in page_ids:
            rec, pv = rec_by_page[pid], pages_v1[pid]
            img = _resolve(root, rec["page"]["image_path"])
            pages.append({
                "page_id": pid, "page_no": rec["page"]["page_no"], "width": rec["page"]["width"], "height": rec["page"]["height"],
                "coordinate_system": "page_px_xyxy",
                "image": {"sha256": rec["page"]["image_sha256"], "path": os.path.relpath(img, file_dir).replace("\\", "/")},
                "route": rec["route"]["route"], "execution_status": pv["execution_status"], "quality_status": pv["quality_status"],
                "boundary": pv.get("boundary"),
                "issues": [{"code": i["code"], "severity": i["severity"], "message": i["message"]}
                           for i in rec["issues"] if not i["code"].startswith(EVAL_ISSUE_PREFIX)],
            })
        ids = set(page_ids)
        regions = [_region(r) for r in result_v1["regions"] if r["page_id"] in ids]
        unassigned = [_unassigned(u) for u in result_v1["unassigned_texts"] if u["page_id"] in ids]
        read_issues = [{"page_id": i["page_id"], "code": i["code"], "severity": i["severity"], "message": i["message"]}
                       for i in result_v1["issues"] if i["page_id"] in ids
                       and i["code"] in ("REGION_READ_FAILED", "TABLE_CHECK_UNKNOWN")]
        for p in pages:
            p["issues"] += [{k: v for k, v in i.items() if k != "page_id"} for i in read_issues if i["page_id"] == p["page_id"]]
        source = {"file_id": file_id, "file_name": src.get("source_name"), "input_order": order[f["submission_id"]].index(file_id) + 1,
                  "file_type": src.get("kind"), "sha256": src.get("source_sha256"),
                  "page_count": src.get("page_count") or len(page_ids)}
        rid = result_id(source["sha256"] or "", profile, _ocr_hashes(root, calls, ids),
                        _vlm_hashes(trace_dir, {r["region_id"] for r in regions}))
        docs.append({
            "contract": CONTRACT, "result_id": rid, "submission_id": f["submission_id"], "source": source,
            "parser_profile": profile, "pages": pages, "regions": regions, "unassigned_texts": unassigned, "products": [],
            "trace_ref": {"run_dir": os.path.relpath(trace_dir.parent, file_dir).replace("\\", "/"),
                          "reads": os.path.relpath(trace_dir / "reads.json", file_dir).replace("\\", "/"),
                          "stage1_records": [os.path.relpath(stage1_dir / "records" / f"{p}.json", file_dir).replace("\\", "/")
                                             for p in page_ids]},
        })
    return docs


def write_file_results(out_dir: Path, docs: list[dict]) -> list[dict]:
    """files/<file_id>/result.json 과 files/index.json(입력 순서 목록)을 쓴다. 같은 file_id 가 두 번 나오면 오류."""
    seen = set()
    index = []
    for d in docs:
        fid = d["source"]["file_id"]
        if fid in seen:
            raise ValueError(f"중복 file_id {fid}: 파일별 결과가 서로 덮어쓰게 된다")
        seen.add(fid)
        path = out_dir / "files" / fid / "result.json"
        dump(path, d)
        rs = d["regions"]
        index.append({"submission_id": d["submission_id"], "input_order": d["source"]["input_order"], "file_id": fid,
                      "file_name": d["source"]["file_name"], "file_type": d["source"]["file_type"],
                      "pages": len(d["pages"]), "regions": len(rs), "unassigned_texts": len(d["unassigned_texts"]),
                      "result_id": d["result_id"], "result": f"files/{fid}/result.json",
                      "execution_status": sorted({p["execution_status"] for p in d["pages"]}),
                      "text_status": {k: sum(r["text_status"] == k for r in rs) for k in ("read", "no_text", "read_failed", "not_read")},
                      "review_required": sum(r["review"]["required"] for r in rs),
                      "review_quality": sum(bool(r["review"]["quality_reasons"]) for r in rs)})
    dump(out_dir / "files" / "index.json", {"contract": CONTRACT + "/index", "files": index})
    return index


def export_run(root: Path, run_dir: Path, out_dir: Path) -> list[dict]:
    """기존 실행(layout-result/1)을 파일별 layout-result/2 로 내보낸다. run_dir 은 읽기만 한다."""
    result_v1 = load(run_dir / "result.json")
    stage1_dir = _resolve(root, result_v1["settings"]["stage1"])
    records = [load(stage1_dir / "records" / f"{p['page_id']}.json") for p in result_v1["pages"]]
    from .supplement import reapply
    reapply(records, run_dir / "trace")                  # 보충 영역(…-S000)은 trace 근거로 다시 더한다
    docs = build_file_results(root, out_dir, result_v1, records, stage1_dir, run_dir / "trace")
    return write_file_results(out_dir, docs)
