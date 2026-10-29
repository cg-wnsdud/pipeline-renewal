"""통합 실행: 입력 → 페이지 준비 → OCR → 영역/배정/미배정 → 모든 Region VLM 판독 → 최종 JSON·trace·대조 화면.

예(기본 프로필, 옵션은 입력과 출력 폴더만):
  .venv/Scripts/python.exe scripts/run_pipeline.py --manifest <manifest.json> --out work/pipeline/<새 폴더>
  # 이미 있는 Stage 1 실행을 재사용(OCR 호출 없이 판독만)
  .venv/Scripts/python.exe scripts/run_pipeline.py --from-stage1 work/runs/stage1-merge-large --out work/pipeline/full-r1

출력:
  <out>/result.json        최종 결과(layout-result/1): 페이지·Region(bbox·최종 문구·상태·표·검토 근거)·미배정 OCR·이슈
  <out>/trace/reads.json   Region 별 판독 근거(OCR 참고·검토 측정값·계약 오류·호출 ID)
  <out>/trace/vlm/*.json   VLM 요청 메타(프롬프트·스키마·이미지 크기/해시)와 원시 응답
  <out>/trace/timing.json  단계별 시간·동시성
  <out>/stage1/            (OCR 을 새로 실행한 경우) Stage 1 산출물
  <out>/viewer/index.html  최종 대조 화면
  <out>/files/             파일별 파싱 snapshot(layout-result/2)
  <out>/semantic/          (기본, --no-semantic 이면 생략) snapshot 저장 뒤 의미 단계: 상품·템플릿 분류·영역 배정·라벨링
                           → semantic/files/<file_id>/result.json(layout-result/3)·index.html (docs/14)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import stage1  # noqa: E402
from layout_pipeline.assignment import OVERLAP_RULE, AssignmentRule  # noqa: E402
from layout_pipeline.config import get_settings, load_env_file  # noqa: E402
from layout_pipeline.final_output import build_result  # noqa: E402
from layout_pipeline import contract_v2  # noqa: E402
from layout_pipeline.file_result import build_file_results, write_file_results  # noqa: E402
from layout_pipeline.final_viewer import build_file_viewer, build_final_viewer  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline import classify  # noqa: E402
from layout_pipeline.pages import template_requests  # noqa: E402
from layout_pipeline.read_stage import Reader  # noqa: E402
from layout_pipeline import vlm_tasks  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402
from layout_pipeline.vlm_tasks import (GENERAL_REQUEST_VERSIONS, MARGIN_RULE, TABLE_REQUEST_VERSION,  # noqa: E402
                                       TABLE_REQUEST_VERSIONS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", action="append", type=Path, default=[])
    ap.add_argument("--from-stage1", type=Path, default=None, help="기존 Stage 1 실행 폴더 재사용(OCR 호출 없음)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rebuild", action="store_true", help="저장된 trace/vlm 원시 응답으로 result.json·화면만 다시 만듦(호출 없음)")
    # 기본 프로필(page-first-default/1, docs/12): 아래 기본값만으로 실행한다(--manifest 와 --out 만 지정).
    ap.add_argument("--boundary-plans", default=str(ROOT / "fixtures" / "boundary-plans"),
                    help="승인된 긴 이미지 경계 문서 폴더(기본 fixtures/boundary-plans, 이미지 해시가 같은 페이지에만 적용). none 이면 쓰지 않음")
    ap.add_argument("--run-candidate-boundaries", action=argparse.BooleanOptionalAction, default=True,
                    help="(기본 켬) 자동 경계가 정책 검증 범위 밖이라 후보로 남은 긴 이미지도 그 경계로 실행해 결과를 낸다"
                         "(CANDIDATE_RUN·candidate_unvalidated 로 표시, 확정 결과 아님). 끄면 그런 페이지는 보류")
    ap.add_argument("--auto-boundaries", action=argparse.BooleanOptionalAction, default=True,
                    help="(기본 켬) 확정 경계 문서가 없는 긴 이미지에 자동 경계 정책(section-pack/2, config/boundary-policy.json) 적용")
    ap.add_argument("--reuse-ocr", type=Path, default=None,
                    help="같은 페이지·같은 입력의 기존 Stage 1 OCR 원시 응답 재사용(새 입력만 OCR)")
    ap.add_argument("--pages", default="", help="쉼표로 구분한 page_id 만 판독(--from-stage1 와 함께 쓰면 OCR 없음)")
    ap.add_argument("--general-crop-margin", choices=["none", "ocr-median-0.3"], default="none",
                    help="일반 영역 crop 여백(기본 none: 여백 비교에서 ocr-median-0.3 이 누락 증가로 탈락)")
    ap.add_argument("--general-request", choices=GENERAL_REQUEST_VERSIONS, default="crop/1",
                    help="일반 영역 판독 지시(crop/2 = 판독 원칙 v2: 이미지 기준 읽기 순서 + 정렬된 OCR 후보 참고)")
    ap.add_argument("--table-request", choices=TABLE_REQUEST_VERSIONS, default=TABLE_REQUEST_VERSION)
    ap.add_argument("--read-mode", choices=["crop", "page-first", "page-first-noocr"], default="page-first",
                    help="page-first(기본, docs/12 프로필): 페이지(타일·띠) 전체 판독 + 일반 조건 영역만 원본 crop 재판독. "
                         "crop: 영역 crop 묶음 판독(이전 기본, 비교용)")
    ap.add_argument("--stage1-only", action="store_true", help="Stage 1(OCR·경계·영역·배정)까지만 실행")
    ap.add_argument("--resume", action="store_true",
                    help="page-first: 중단된 같은 출력 폴더의 페이지 판독 응답을 재사용(--from-stage1 필요)")
    ap.add_argument("--verify-mode", choices=["reread", "patch"], default="reread",
                    help="page-first 영역 확인: reread(region-reread/1, docs/10) | patch(region-patch/1, 이전 실행 재현용)")
    ap.add_argument("--table-read", choices=["legacy", "cells"], default="cells",
                    help="표 판독: cells(기본, table-cells/1, docs/11: text·셀 구조를 한 응답으로) | legacy(table-check/3·page/2 tables, 비교용)")
    ap.add_argument("--max-images", type=int, default=8)
    ap.add_argument("--vlm-concurrency", type=int, default=2)
    ap.add_argument("--ocr-concurrency", type=int, default=1)
    ap.add_argument("--assignment-rule", choices=["strict", "overlap-best"], default="strict",
                    help="strict(기본) 또는 겹치는 OCR 줄을 최적 단일 영역에 귀속하는 overlap-best/1")
    ap.add_argument("--layout-config", default=str(ROOT / "config" / "layout-options.json"))
    ap.add_argument("--cache-bust", action="store_true", help="성능 비교용: 요청마다 고유 식별자로 서버 prefix cache 재사용 방지")
    ap.add_argument("--supplement", action=argparse.BooleanOptionalAction, default=True,
                    help="(기본 켬) 판독 뒤 놓친 영역 보충(docs/16 §9, supplement/1): 레이아웃 상자 밖 OCR 묶음과 낮은 문턱(0.3) 표 상자로 "
                         "보충 영역을 만들어 원본에서 읽고, 기존 영역과 중복이 아니면 더한다. 끄면 레이아웃 상자 영역만")
    # docs/18 문구 순서 오류 수정 비교. 기본값은 이전 동작(비교 결과로 정한다).
    ap.add_argument("--ocr-candidate-order", choices=["text", "position"], default="text",
                    help="VLM 에 주는 OCR 후보 목록 순서: text(문자열 순 정렬) | position(위→아래, 같은 줄 왼쪽→오른쪽)")
    ap.add_argument("--supplement-reread", action=argparse.BooleanOptionalAction, default=False,
                    help="보충 문자 영역도 본 판독과 같은 확인 조건에 걸리면 재판독(region-reread/1)")
    ap.add_argument("--table-text-fallback", action=argparse.BooleanOptionalAction, default=False,
                    help="표 셀 재판독이 셀 구조만 실패하면 그 문구를 쓰고 표 구조는 없음(text-only)으로 둔다")
    ap.add_argument("--semantic", action=argparse.BooleanOptionalAction, default=True,
                    help="(기본 켬) 파싱 snapshot 저장 뒤 상품·템플릿 분류·영역 배정·라벨링(docs/14, <out>/semantic). "
                         "같은 --vlm-concurrency 상한을 쓴다. 끄면 파싱까지만")
    args = ap.parse_args(argv)
    if args.rebuild:
        return rebuild(args.out)
    if args.out.exists() and any(args.out.iterdir()) and not args.resume:
        ap.error(f"출력 폴더가 비어 있지 않습니다(측정 덮어쓰기 방지): {args.out}")
    load_env_file()
    timing = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    t0 = time.perf_counter()
    if args.from_stage1:
        stage1_dir = args.from_stage1
        timing["stage1"] = {"reused": str(stage1_dir), "ocr_wall_seconds": load(stage1_dir / "run.json").get("wall_seconds")}
        manifests = [Path(m) for m in load(stage1_dir / "run.json").get("manifests") or [] if Path(m).is_file()]
    else:
        if not args.manifest:
            ap.error("--manifest 또는 --from-stage1 이 필요합니다")
        manifests = args.manifest
    # 등록 템플릿 요청(docs/21): 모르는 템플릿이면 OCR·모델 호출 전에 멈춘다
    requests = template_requests(manifests)
    for ids in requests.values():
        classify.template_request(ids)
    if not args.from_stage1:
        stage1_dir = args.out / "stage1"
        layout_options = {}
        if args.layout_config != "none":
            layout_options = json.loads(Path(args.layout_config).read_text(encoding="utf-8"))["layout_options"]
        stage1.run(args.manifest, stage1_dir, get_settings(), pdf_dpi=200, policy_path=ROOT / "config" / "routing.json",
                   plans_dir=None if args.boundary_plans == "none" else Path(args.boundary_plans), layout_options=layout_options,
                    only_pages={p for p in args.pages.split(",") if p} or None,
                    rule=OVERLAP_RULE if args.assignment_rule == "overlap-best" else AssignmentRule(),
                   ocr_concurrency=args.ocr_concurrency, run_candidates=args.run_candidate_boundaries,
                   reuse_dir=args.reuse_ocr,
                   auto_vlm=VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"],
                                      float(os.environ.get("PN_VLM_TIMEOUT_S", "320")), prefix="bnd")
                   if args.auto_boundaries else None, vlm_concurrency=args.vlm_concurrency)
        from layout_pipeline.viewer import build_viewer
        build_viewer(stage1_dir)
        s1 = load(stage1_dir / "run.json")
        timing["stage1"] = {"ocr_concurrency": args.ocr_concurrency, "wall_seconds": round(time.perf_counter() - t0, 3),
                            "reused_ocr_calls": s1.get("reused_ocr_calls"), "new_ocr_calls": s1.get("new_ocr_calls"),
                            # stage1 벽시계 중 자동 경계 VLM 관계 판정 시간(라운드 안 동시 --vlm-concurrency)
                            "auto_boundary_judge_seconds": round(sum(a["judge_seconds"] for a in s1.get("auto_boundary_timing") or []), 3)}
    if args.stage1_only:
        print(json.dumps({"stage1": str(stage1_dir), "timing": timing}, ensure_ascii=False, indent=1))
        return 0
    page_ids = [p["page_id"] for p in load(stage1_dir / "pages.json")]
    if args.pages:
        page_ids = [p for p in page_ids if p in set(args.pages.split(","))]
    records = [load(stage1_dir / "records" / f"{pid}.json") for pid in page_ids]
    nonce = uuid.uuid4().hex[:8] if args.cache_bust else None
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                       cache_bust=nonce)
    if args.general_request in ("crop/2", "crop/3") and args.general_crop_margin != "none":
        ap.error(f"{args.general_request} 는 여백 없는 crop 에서만 정의됨")
    vlm_tasks.CANDIDATE_ORDER = args.ocr_candidate_order
    reader = Reader(client, args.out / "trace", args.vlm_concurrency, args.general_request, args.table_request)
    reader.verify_mode = args.verify_mode
    reader.table_read = args.table_read
    reader.table_text_fallback = args.table_text_fallback
    if args.resume:
        if not args.from_stage1 or args.read_mode == "crop":
            ap.error("--resume 는 --from-stage1 과 page-first 판독에서만 쓴다")
        timing["resumed_page_calls"] = reader.load_resume()
    t1 = time.perf_counter()
    if args.read_mode == "crop":
        reads = reader.read_pages(records, args.max_images, args.general_crop_margin)
    else:
        reads = reader.read_pages_page_first(records, args.max_images, with_ocr=args.read_mode == "page-first")
    timing["read"] = {"vlm_concurrency": args.vlm_concurrency, "wall_seconds": round(time.perf_counter() - t1, 3),
                      "http_calls": client.count}
    if args.supplement:
        from layout_pipeline import supplement
        t_s, n0 = time.perf_counter(), client.count
        sup_reader = Reader(client, args.out / "trace", args.vlm_concurrency, "crop/3", args.table_request)
        sup_reader.table_read = "cells"
        probe = supplement.TableProbe(load(stage1_dir / "run.json").get("layout_options"), args.out / "trace")
        sup = supplement.run(records, reads, sup_reader, probe, args.out / "trace", args.max_images, reread=args.supplement_reread)
        timing["supplement"] = {"wall_seconds": round(time.perf_counter() - t_s, 3), "ocr_calls": sup["ocr_calls"],
                                "vlm_calls": client.count - n0, "added": sup["added"], "dropped": sup["dropped"]}
    settings = {"stage1": str(stage1_dir), "layout_options": load(stage1_dir / "run.json").get("layout_options"),
                "assignment_rule": load(stage1_dir / "run.json").get("assignment_rule"),
                "general_request": args.general_request if args.general_crop_margin == "none" else "crop-margin/1",
                "general_crop_margin": args.general_crop_margin if args.general_crop_margin == "none" else MARGIN_RULE,
                "table_request": args.table_request, "max_images_per_request": args.max_images,
                "vlm_concurrency": args.vlm_concurrency, "temperature": 0, "cache_bust": nonce,
                "read_mode": args.read_mode, "run_candidate_boundaries": args.run_candidate_boundaries,
                "table_read": args.table_read if args.table_read == "legacy" else "table-cells/1",
                "ocr_candidate_order": vlm_tasks.CANDIDATE_ORDER_VERSIONS[args.ocr_candidate_order],
                "table_text_fallback": args.table_text_fallback}
    if args.supplement:
        settings["supplement"] = supplement.settings() | {"reread": args.supplement_reread}
    if args.read_mode != "crop":
        from layout_pipeline import page_read
        settings |= {"page_request": page_read.PAGE_VERSION if args.read_mode == "page-first" else page_read.PAGE_VERSION_NO_OCR,
                     "verify_request": page_read.REREAD_VERSION if args.verify_mode == "reread" else page_read.PATCH_VERSION,
                     "reread_select": page_read.REREAD_SELECT_VERSION if args.verify_mode == "reread" else None,
                     "general_request": None,
                     "page_max_tokens_rule": page_read.PAGE_TOKENS_RULE, "page_partial": page_read.PAGE_PARTIAL_VERSION,
                     "verify_conditions": {"min_effective_text_px": page_read.MIN_EFFECTIVE_TEXT_PX,
                                           "missing_ocr_cover": page_read.MISSING_OCR_COVER,
                                           "reading_order": page_read.ORDER_CHECK_VERSION}}
    result, regions = build_result(records, reads, settings)
    dump(args.out / "result.json", result)
    dump(args.out / "trace" / "reads.json", {r["region_id"]: {k: r.get(k) for k in ("trace", "contract_errors", "uncertain", "review")}
                                            for r in regions})
    timing["total_wall_seconds"] = round(time.perf_counter() - t0, 3)
    dump(args.out / "trace" / "timing.json", timing)
    viewer = build_final_viewer(args.out, stage1_dir)
    files = export_files(args.out, result, records, stage1_dir)
    summary = {"contract_checks": result["contract_checks"]["passed"], "problems": result["contract_checks"]["problems"][:5],
               "file_results": files,
               "pages": [(p["page_id"], p["execution_status"], p["reading"]) for p in result["pages"]],
               "timing": timing, "viewer": str(viewer)}
    if args.semantic:
        t2 = time.perf_counter()
        summary["semantic"] = run_semantic_stage(args.out, args.vlm_concurrency, requests)
        timing["semantic"] = summary["semantic"].get("timing")
        timing["semantic_stage_wall_seconds"] = round(time.perf_counter() - t2, 3)
    # 입력 → 파싱 → 결과 저장·화면 → 의미 단계까지 한 실행의 전체 경과(total_wall_seconds 는 파싱까지)
    timing["end_to_end_wall_seconds"] = round(time.perf_counter() - t0, 3)
    dump(args.out / "trace" / "timing.json", timing)
    if args.semantic and (args.out / "semantic" / "report.json").is_file():
        from layout_pipeline.report_html import build_report
        build_report(args.out / "semantic")      # 파싱 시간(trace/timing.json)을 더해 입력 검토 화면을 다시 만든다
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def run_semantic_stage(out: Path, concurrency: int, template_requests: dict[str, list[str]] | None = None) -> dict:
    """파싱 snapshot(<out>/files) 저장 뒤 의미 단계(docs/14). 기존 <out>/semantic 이 있으면 덮어쓰지 않고 건너뛴다."""
    from layout_pipeline import semantic
    sem_dir = out / "semantic"
    if sem_dir.exists() and any(sem_dir.iterdir()):
        return {"skipped": f"{sem_dir} 가 이미 있음(덮어쓰지 않음). scripts/run_semantic.py --parse {out} --out <새 폴더> 로 실행"}
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                       prefix="sem")
    report = semantic.run(out, sem_dir, client, concurrency, template_requests=template_requests)
    return {"checks": report["checks"], "timing": report["timing"], "viewer": report["viewer"],
            "products": {s["submission_id"]: s["products"] for s in report["submissions"]}}


def rebuild(out: Path) -> int:
    from layout_pipeline.read_stage import reconstruct_reads
    old = load(out / "result.json")
    stage1_dir = Path(old["settings"]["stage1"])
    page_ids = [p["page_id"] for p in old["pages"]]
    records = [load(stage1_dir / "records" / f"{pid}.json") for pid in page_ids]
    from layout_pipeline import page_read, supplement
    supplement.reapply(records, out / "trace")      # 보충 영역(채택분)을 저장 근거대로 다시 더한다(호출 없음)
    reads = reconstruct_reads(out / "trace", records)
    settings = old["settings"] | {"rebuilt_from_trace": True}
    if settings.get("verify_request") in page_read.REREAD_VERSIONS:      # 재구성은 지금 코드의 선택 정책을 쓴다
        settings["reread_select"] = page_read.REREAD_SELECT_VERSION
    result, regions = build_result(records, reads, settings)
    dump(out / "result.json", result)
    dump(out / "trace" / "reads.json", {r["region_id"]: {k: r.get(k) for k in ("trace", "contract_errors", "uncertain", "review")}
                                        for r in regions})
    build_final_viewer(out, stage1_dir)
    files = export_files(out, result, records, stage1_dir)
    sem = None
    if (out / "semantic" / "run.json").is_file():      # 의미 단계도 저장 응답으로 다시 만든다(호출 없음)
        from layout_pipeline import semantic
        sem = semantic.rebuild(out / "semantic")["checks"]
    print(json.dumps({"contract_checks": result["contract_checks"]["passed"], "problems": result["contract_checks"]["problems"][:5],
                      "file_results": files, "semantic_checks": sem}, ensure_ascii=False))
    return 0


def export_files(out: Path, result: dict, records: list[dict], stage1_dir: Path) -> dict:
    """입력 파일별 layout-result/2(docs/12): files/<file_id>/result.json, files/index.json, files/index.html."""
    docs = build_file_results(ROOT, out, result, records, stage1_dir, out / "trace")
    index = write_file_results(out, docs)
    errors = {d["source"]["file_id"]: contract_v2.check_document(d) for d in docs}
    sub_errors = contract_v2.check_submission(docs)
    checks = {"passed": not any(errors.values()) and not sub_errors,
              "errors": {k: v for k, v in errors.items() if v}, "submission_errors": sub_errors}
    dump(out / "files" / "contract-check.json", checks)
    viewer = build_file_viewer(out, docs, checks)
    return {"files": len(index), "contract_passed": checks["passed"], "index": str(out / "files" / "index.json"),
            "viewer": str(viewer)}


if __name__ == "__main__":
    raise SystemExit(main())
