"""파서 서비스 실행(parser-service/1, docs/21): 요청 하나(광고 한 건의 파일들 + 등록 템플릿) → 실행 폴더 + service.json.

nh-ad-compliance 파서 서비스(apps/parser/app.py)가 scripts/run_service.py 를 별도 프로세스로 실행하고 실행 폴더를 ZIP 으로 돌려준다.
실행은 scripts/run_pipeline.py 기본 프로필 그대로다(같은 입력이면 같은 결과). 이 모듈은 요청 검사·manifest 작성·상태 판정만 한다.

요청(request.json):
  {"submission_id": "<광고 묶음 ID>", "files": [{"file_id": "<파일 ID>", "path": "<원본 경로>"}, …(페이지 순서)],
   "template_request": {"template_ids": ["<등록 템플릿 ID>", …]}}        // 없거나 빈 목록이면 자동 배정
  ID 는 [A-Za-z0-9][A-Za-z0-9_-]{0,79}(페이지·영역 ID 와 파일 경로에 들어간다). 결과의 파일명은 원본 경로의 파일명이다.

service.json(실행 폴더 맨 위). 경로는 실행 폴더 기준:
  contract, status: completed | partial | failed, failure: null | {code, message}
  review_input: 심의 입력(review-input/2) 경로(실패하면 null), request: 요청 요약(파일 ID·파일명·등록 템플릿)
  files: [{file_id, file_name, result(layout-result/3 경로), semantic_status}], pages: [{page_id, file_id, page_no, execution_status}]
  regions: {total, read_failed}, checks: {parse, semantic, review_input}, contracts, timing
상태 판정:
  failed  — 계약 검사 실패(CONTRACT_CHECK_FAILED, 종료 코드 4) · 처리 못 한 쪽(PAGES_NOT_PROCESSED, 3) · 모든 영역 판독 실패
            (READ_FAILED, 3) · 의미 단계 실패(SEMANTIC_FAILED, 3). 3 은 OCR·VLM 서버 문제일 수 있어 다시 시도할 수 있다.
  partial — 결과는 계약을 지키지만 의미 단계 일부(통합·라벨 호출)가 실패. 판정은 할 수 있다(종료 코드 0).
  completed — 모든 쪽 처리, 의미 단계 완료, 계약 검사 통과(0).
  입력 오류(요청 형식·지원하지 않는 형식·HWP 변환 실패·모르는 템플릿)는 종료 코드 2, 그 밖의 예외는 1 이다.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import classify, handoff
from . import semantic_assets as A
from .file_result_v3 import CONTRACT as RESULT_CONTRACT
from .jsonio import dump, load

CONTRACT = "parser-service/1"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".hwp", ".hwpx"}
PROCESSED = ("READY", "CANDIDATE_RUN")
EXIT = {"completed": 0, "partial": 0, "INPUT_INVALID": 2, "PAGES_NOT_PROCESSED": 3, "READ_FAILED": 3, "SEMANTIC_FAILED": 3,
        "CONTRACT_CHECK_FAILED": 4, "INTERNAL_ERROR": 1}


class RequestError(ValueError):
    """요청을 처리할 수 없음(형식·ID·파일·템플릿). 입력 오류로 돌려준다."""


def supported_templates() -> list[str]:
    """등록 템플릿 요청으로 받을 수 있는 템플릿 ID(분류 정책 순서)."""
    return [t["template_id"] for t in A.templates()]


def validate(request: dict) -> dict:
    """요청 검사 → {submission_id, files: [{file_id, path, file_name}], template_ids}."""
    if not isinstance(request, dict):
        raise RequestError("요청은 JSON 객체여야 합니다")
    sub = request.get("submission_id")
    if not isinstance(sub, str) or not ID_PATTERN.fullmatch(sub):
        raise RequestError("submission_id 형식이 잘못되었습니다")
    items = request.get("files")
    if not isinstance(items, list) or not items:
        raise RequestError("files 가 비었습니다")
    files, seen = [], set()
    for it in items:
        fid = it.get("file_id") if isinstance(it, dict) else None
        if not isinstance(fid, str) or not ID_PATTERN.fullmatch(fid):
            raise RequestError("file_id 형식이 잘못되었습니다")
        if fid in seen:
            raise RequestError(f"중복 file_id: {fid}")
        seen.add(fid)
        path = Path(str(it.get("path") or ""))
        if not path.is_file():
            raise RequestError(f"{fid}: 입력 파일이 없습니다")
        if path.suffix.lower() not in SUFFIXES:
            raise RequestError(f"{fid}: 지원하지 않는 형식입니다({path.suffix})")
        files.append({"file_id": fid, "path": str(path.resolve()), "file_name": path.name})
    req = request.get("template_request") or {}
    ids = req.get("template_ids") if isinstance(req, dict) else None
    ids = [] if ids is None else ids
    if not isinstance(ids, list) or not all(isinstance(t, str) and t for t in ids):
        raise RequestError("template_request.template_ids 는 템플릿 ID 문자열 목록이어야 합니다")
    try:
        classify.template_request(ids)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    return {"submission_id": sub, "files": files, "template_ids": list(dict.fromkeys(ids))}


def write_manifest(req: dict, path: Path) -> Path:
    """검사한 요청 → run_pipeline 입력 정의(manifest)."""
    entry = {"submission_id": req["submission_id"], "files": [{"file_id": f["file_id"], "path": f["path"]} for f in req["files"]]}
    if req["template_ids"]:
        entry["template_request"] = {"template_ids": req["template_ids"]}
    dump(path, {"manifest_version": 1, "note": "parser-service request", "source_root": str(path.parent), "submissions": [entry]})
    return path


def _passed(path: Path) -> bool | None:
    return bool(load(path).get("passed")) if path.is_file() else None


def assess(out: Path, req: dict) -> dict:
    """실행 폴더 → service.json 내용(상태·실패 코드·경로·쪽 상태·검사)."""
    sub = req["submission_id"]
    sem = out / "semantic"
    files, pages, total, failed_reads, sem_status = [], [], 0, 0, []
    index = load(sem / "files" / "index.json")["files"] if (sem / "files" / "index.json").is_file() else []
    for f in sorted(index, key=lambda f: f["input_order"]):
        d = load(sem / f["result"])
        files.append({"file_id": f["file_id"], "file_name": f["file_name"], "result": f"semantic/{f['result']}",
                      "semantic_status": d["semantic"]["status"]})
        pages += [{"page_id": p["page_id"], "file_id": f["file_id"], "page_no": p["page_no"], "execution_status": p["execution_status"]}
                  for p in d["pages"]]
        total += len(d["regions"])
        failed_reads += sum(r["text_status"] == "read_failed" for r in d["regions"])
        sem_status.append(d["semantic"]["status"])
    checks = {"parse": _passed(out / "files" / "contract-check.json"), "semantic": _passed(sem / "files" / "contract-check.json"),
              "review_input": _passed(sem / "handoff" / "check.json")}
    review_input = sem / "handoff" / f"{sub}.json"
    expected = {f["file_id"] for f in req["files"]}
    if not all(checks.values()) or not review_input.is_file() or {f["file_id"] for f in files} != expected:
        failure = ("CONTRACT_CHECK_FAILED", f"계약 검사 {checks}, 파일 {sorted(f['file_id'] for f in files)}")
    elif any(p["execution_status"] not in PROCESSED for p in pages):
        failure = ("PAGES_NOT_PROCESSED", ", ".join(f"{p['page_id']}={p['execution_status']}" for p in pages
                                                    if p["execution_status"] not in PROCESSED))
    elif total and failed_reads == total:
        failure = ("READ_FAILED", f"영역 {total}개 판독이 모두 실패")
    elif "failed" in sem_status:
        failure = ("SEMANTIC_FAILED", "의미 단계(상품·템플릿 분류) 호출 실패")
    else:
        failure = None
    status = "failed" if failure else "partial" if "partial" in sem_status else "completed"
    timing = load(out / "trace" / "timing.json") if (out / "trace" / "timing.json").is_file() else {}
    return {"contract": CONTRACT, "status": status,
            "failure": {"code": failure[0], "message": failure[1]} if failure else None,
            "review_input": f"semantic/handoff/{sub}.json" if not failure else None,
            "contracts": {"result": RESULT_CONTRACT, "review_input": handoff.CONTRACT},
            "request": {"submission_id": sub, "files": [{"file_id": f["file_id"], "file_name": f["file_name"]} for f in req["files"]],
                        "template_request": {k: v for k, v in classify.template_request(req["template_ids"]).items() if k != "codes"}},
            "files": files, "pages": pages, "regions": {"total": total, "read_failed": failed_reads}, "checks": checks,
            "timing": {"end_to_end_wall_seconds": timing.get("end_to_end_wall_seconds"),
                       "semantic_stage_wall_seconds": timing.get("semantic_stage_wall_seconds")}}


def failure_doc(req: dict | None, code: str, message: str) -> dict:
    """실행 전·중 예외로 결과 폴더를 판정할 수 없을 때의 service.json."""
    return {"contract": CONTRACT, "status": "failed", "failure": {"code": code, "message": message[:2000]}, "review_input": None,
            "contracts": {"result": RESULT_CONTRACT, "review_input": handoff.CONTRACT},
            "request": {"submission_id": req["submission_id"], "files": [{"file_id": f["file_id"], "file_name": f["file_name"]}
                                                                         for f in req["files"]]} if req else None}


def exit_code(doc: dict) -> int:
    return EXIT[doc["status"]] if doc["status"] != "failed" else EXIT.get(doc["failure"]["code"], 1)
