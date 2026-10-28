"""1단계 실행: 입력 → 경로 → OCR(전체 페이지 또는 확정 타일) → 영역 → 배정/미배정 → 진단.

OCR 은 전역 직렬(동시 1)로 호출한다. 빌드 단계(build_page)는 저장된 원시 응답만으로 다시 실행할 수
있으며(rebuild), 그 경우 새 측정 호출로 세지 않는다.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


from . import diagnostics, gold_eval, vlm_contract
from .assignment import AssignmentRule, assign
from .geometry import area
from .config import PROJECT_ROOT, Settings
from .jsonio import dump, load
from .ocr_client import BASE_OPTIONS, OcrClient
from .pages import Page, load_manifest, render_submission
from .regions import parse_view
from .routing import RouteDecision, decide, load_plans, load_policy

CONTRACT = "layout-stage1/1"
LARGE_REGION_FRAC = 0.25   # 진단 표시 기준. 영역을 바꾸지 않는다.
# 문서 틀 표(docs/19): 레이아웃이 표로 검출했지만 OCR 글자가 이만큼 이상이면 본문 전체를 담은 틀로 보고 글 영역으로 읽는다.
# 기존 실행의 표 후보 52개 중 실제 표는 800자 미만, 본문 틀(HWP t07 4개, whole-test 대출 광고2 1쪽)은 950~2,200자였다.
CONTAINER_TABLE_MIN_CHARS = 900
CONTAINER_RULE = "table-container-as-text/1"


def _issue(code: str, severity: str, message: str, refs: list | None = None) -> dict:
    return {"code": code, "severity": severity, "message": message, "refs": refs or []}


def build_page(page: Page, decision: RouteDecision, calls: list[dict], responses: dict[str, dict],
               rule: AssignmentRule) -> dict:
    """원시 응답(view_id → prunedResult)에서 페이지 기록을 만든다."""
    issues = []
    regions, items, blocks = [], [], []
    for view in decision.views:
        pruned = responses.get(view.view_id)
        if pruned is None:
            issues.append(_issue("OCR_CALL_FAILED", "error", f"{view.view_id} OCR 응답 없음",
                                 [c["call_id"] for c in calls if c["view_id"] == view.view_id]))
            continue
        r, i, b = parse_view(pruned, view)
        regions += r
        items += i
        blocks += b
    status = decision.status
    executed = status in ("READY", "CANDIDATE_RUN")
    if executed and any(x["code"] == "OCR_CALL_FAILED" for x in issues):
        status = "OCR_FAILED"
        executed = False
    elif status == "CANDIDATE_RUN":
        issues.append(_issue("BOUNDARY_CANDIDATE_RUN", "review", decision.basis))
    elif status != "READY":
        issues.append(_issue(status, "pending", decision.basis))
    assignment = assign(items, regions, rule)
    rel = diagnostics.region_text_relations(regions, assignment)
    overlaps = diagnostics.region_overlaps(regions)
    tile_edges = diagnostics.tile_edge_regions(regions, decision.views, page.width, page.height)
    region_rows = []
    containers = []
    for r in regions:
        texts = [a["ocr_text"] for a in assignment["assigned"] if a["region_id"] == r.region_id]
        row = r.to_dict() | {"assigned_ocr_ids": rel[r.region_id]["assigned"],
                             "crossing_ocr_ids": rel[r.region_id]["crossing"],
                             "ambiguous_ocr_ids": rel[r.region_id]["ambiguous"],
                             "ocr_draft": "\n".join(t for t in texts if t.strip()),
                             "text": None, "text_status": "not_read"}
        chars = sum(len(t.strip()) for t in texts)
        if row["table_candidate"] and chars >= CONTAINER_TABLE_MIN_CHARS:
            # 문서 틀 표(docs/19): 본문 전체를 담은 표 상자는 표 셀 판독이 안쪽 작은 표 하나만 읽고 나머지를 버린다. 글 영역으로 읽는다.
            row |= {"table_candidate": False,
                    "table_container": {"rule": CONTAINER_RULE, "ocr_chars": chars, "ocr_lines": len(texts)}}
            containers.append(r.region_id)
        region_rows.append(row)
    if containers:
        issues.append(_issue("TABLE_CONTAINER_READ_AS_TEXT", "info",
                             f"OCR 글자 {CONTAINER_TABLE_MIN_CHARS}자 이상인 표 상자 {len(containers)}개를 글 영역으로 판독", containers))
    if executed:
        nonempty = [u for u in assignment["unassigned_texts"] if not u["empty_text"]]
        weak = [a["ocr_id"] for a in assignment["assigned"] if "low_ocr_overlap" in a.get("assignment_review", [])
                and not a["empty_text"]]
        multiple = [a["ocr_id"] for a in assignment["assigned"]
                    if "multiple_region_overlap" in a.get("assignment_review", []) and not a["empty_text"]]
        if weak:
            issues.append(_issue("OCR_WEAK_OVERLAP_ASSIGNED", "review",
                                 f"겹침이 50% 미만인 영역에 귀속한 OCR 글자 {len(weak)}개", weak))
        if multiple:
            issues.append(_issue("OCR_MULTIPLE_REGIONS_ASSIGNED", "review",
                                 f"여러 영역과 겹쳐 최적 영역 하나에 귀속한 OCR 글자 {len(multiple)}개", multiple))
        if not regions:
            issues.append(_issue("NO_LAYOUT_REGIONS", "review", "레이아웃 검출 영역 0개"))
        for code, reasons, label in [
            ("TEXT_OUTSIDE_LAYOUT", {"outside_layout"}, "원래 bbox 가 어떤 영역과도 겹치지 않는 OCR 글자"),
            ("TEXT_MARGIN_ONLY", {"margin_overlap_only"}, "bbox 여백만 영역에 걸치고 중심부는 영역 밖인 OCR 글자"),
            ("TEXT_PARTIAL_IN_REGION", {"partial_overlap"}, "영역 경계에 걸쳐 일부만 포함된 OCR 글자"),
            ("TEXT_AMBIGUOUS_REGIONS", {"ambiguous_multiple_regions", "ambiguous_overlapping_regions"},
             "여러 영역에 걸쳐 소속이 모호한 OCR 글자")]:
            hit = [u["ocr_id"] for u in nonempty if u["reason"] in reasons]
            if hit:
                issues.append(_issue(code, "review", f"{label} {len(hit)}개", hit))
        if overlaps:
            contained = sum(o["relation"] == "contained" for o in overlaps)
            issues.append(_issue("REGION_OVERLAP", "review",
                                 f"원시 영역 중첩 {len(overlaps)}쌍(포함 {contained}, 부분 겹침 {len(overlaps) - contained})",
                                 [[o["a"], o["b"]] for o in overlaps]))
        if tile_edges:
            issues.append(_issue("TILE_EDGE_REGION", "review", f"타일 내부 경계에 닿은 영역 {len(tile_edges)}개",
                                 [t["region_id"] for t in tile_edges]))
        page_area = page.width * page.height
        large = [r.region_id for r in regions if area(r.bbox) / page_area >= LARGE_REGION_FRAC]
        if large:
            issues.append(_issue("REGION_COVERS_LARGE_AREA", "info",
                                 f"페이지 면적 {LARGE_REGION_FRAC:.0%} 이상 영역 {len(large)}개(과대 컨테이너 여부 확인)", large))
        for c in calls:
            if c.get("text_det_resize", {}) and c["text_det_resize"].get("downscaled"):
                issues.append(_issue("TEXT_DET_DOWNSCALED", "info",
                                     f"{c['view_id']}: 서버 텍스트 검출 입력이 약 {c['text_det_resize']['ratio']}배로 축소됨"
                                     "(max_side_limit, 추정). 분할 조건으로 쓰지 않음", [c["call_id"]]))
    gold = gold_eval.evaluate_page(page.page_id, page.image_sha256, [page.width, page.height], regions, rule) \
        if executed else None
    if gold and gold.get("evaluated"):
        if gold["uncovered"]:
            issues.append(_issue("GOLD_LINES_NOT_COVERED", "review",
                                 f"정답 줄 {len(gold['uncovered'])}/{gold['required_lines']}개가 영역 하나에 들어가지 않음",
                                 [x["gold_id"] for x in gold["uncovered"]]))
        for t in gold["tables"]["gold_tables"]:
            if t["status"] != "single_candidate":
                issues.append(_issue("GOLD_TABLE_NOT_SINGLE_CANDIDATE", "review",
                                     f"정답 표 {t['gold_table']}: {t['status']} (최대 {t['best_fraction']:.0%})",
                                     [t["gold_table"]]))
    # 실행 상태(status)와 내용 품질(quality)을 분리한다. 이 단계에서 품질은 통과로 표시하지 않는다.
    review = sorted({i["code"] for i in issues if i["severity"] in ("review", "error")})
    quality = {"status": "review_required" if review else "not_verified", "reasons": review,
               "note": "READY 는 실행 완료일 뿐이다. 최종 문구(VLM) 검증 전에는 품질 통과로 표시하지 않는다."}
    if status == "CANDIDATE_RUN":
        quality = {"status": "candidate_unvalidated", "reasons": sorted(set(review) | {"BOUNDARY_CANDIDATE_RUN"}),
                   "note": "미검증 후보 경계로 실험 실행한 결과다. 경계·내용 모두 확정 결과가 아니다."}
    elif status != "READY":
        quality = {"status": "not_processed", "reasons": [status], "note": decision.basis}
    record = {
        "page": page.to_dict(), "route": decision.to_dict(), "status": status, "quality": quality,
        "calls": [c["call_id"] for c in calls],
        "regions": region_rows,
        "ocr_items_total": len(items),
        "assignment_rule": assignment["rule"],
        "assignments": assignment["assigned"],
        "unassigned_texts": assignment["unassigned_texts"],
        "partition_checks": assignment["checks"],
        "server_blocks": blocks,
        "diagnostics": {"region_overlaps": overlaps, "tile_edge_regions": tile_edges,
                        "regions_without_text": [r.region_id for r in regions
                                                 if not any(rel[r.region_id].values())],
                        "server_block_comparison": diagnostics.server_block_comparison(regions, blocks)},
        "gold": gold,
        "issues": issues,
    }
    requests = vlm_contract.plan_requests(record)
    record["vlm_plan"] = {"executed": False, "requests": requests,
                          "checks": vlm_contract.validate_requests(requests, region_rows)}
    return record


def run(manifests: list[Path], out: Path, settings: Settings, *, pdf_dpi: int, policy_path: Path,
        plans_dir: Path | None, layout_options: dict | None, only_pages: set[str] | None,
        rule: AssignmentRule, ocr_concurrency: int = 1, run_candidates: bool = False,
        reuse_dir: Path | None = None, auto_vlm=None, vlm_concurrency: int = 1) -> dict:
    """reuse_dir: 같은 페이지·같은 입력 범위(view crop·보낸 이미지 해시·옵션)의 기존 OCR 원시 응답을 재사용한다
    (일반 페이지 전체 OCR 1회 결과를 다시 호출하지 않음). 새 입력(후보 타일 등)만 OCR 한다.
    vlm_concurrency: 자동 경계 관계 판정(auto_vlm)의 라운드 안 동시 호출 상한."""
    started = time.perf_counter()
    policy = load_policy(policy_path)
    plans = load_plans(plans_dir)
    layout_options = validate_layout_options(layout_options or {})
    options = dict(BASE_OPTIONS) | layout_options
    pages: list[Page] = []
    for manifest in manifests:
        for sub in load_manifest(manifest):
            pages += render_submission(sub, out / "pages", pdf_dpi=pdf_dpi, hwp_python=settings.hwp_python,
                                       hwp_project=settings.hwp_project)
    if only_pages:
        pages = [p for p in pages if p.page_id in only_pages]
    decisions = {p.page_id: decide(p, policy, plans, run_candidates) for p in pages}
    client = OcrClient(settings.ocr_url or "", settings.ocr_timeout_s, options, out / "raw", out / "views")
    auto_plans, auto_timing = [], []
    if auto_vlm is not None:
        # 확정 계획이 없는 긴 페이지만 자동 경계 정책(section-pack/2)으로 계획을 만든다(docs/09 §2).
        from . import auto_boundary
        bpol = auto_boundary.load_policy()
        for p in pages:
            d = decisions[p.page_id]
            if d.route == "long" and d.status == "BOUNDARY_PENDING":
                t_scout = time.perf_counter()
                boxes, scalls = auto_boundary.scout(p, client, out)
                t_judge = time.perf_counter()
                plan = auto_boundary.make_plan(p, boxes, auto_vlm, out, bpol, scalls, vlm_concurrency)
                # 탐색 OCR 과 VLM 관계 판정 시간을 나눠 남긴다(stage1 벽시계에 함께 들어간다).
                auto_timing.append({"page_id": p.page_id, "scout_ocr_seconds": round(t_judge - t_scout, 3),
                                    "judge_seconds": round(time.perf_counter() - t_judge, 3),
                                    "judge_calls": sum(len(r["to_judge"]) for r in plan["rounds"]),
                                    "judge_rounds": sum(1 for r in plan["rounds"] if r["to_judge"])})
                dump(out / "boundary-plans" / f"{p.page_id}.json", plan)
                plans[p.page_id] = plan
                auto_plans.append(p.page_id)
                decisions[p.page_id] = decide(p, policy, plans, run_candidates)
    needs_ocr = any(d.views for d in decisions.values())
    if needs_ocr and not settings.ocr_url:
        raise RuntimeError("PN_OCR_URL 이 필요합니다(.env)")
    responses: dict[str, dict] = {}
    reused: list[dict] = []
    reusable = {}
    if reuse_dir is not None:
        for c in load(reuse_dir / "calls.json"):
            if c["ok"]:
                reusable[(c["view_id"], tuple(c["input_crop"]))] = c
        # 새 호출 ID 가 재사용 기록과 겹치지 않게 번호를 이어서 쓴다(탐색 OCR 을 이미 했으면 그 다음부터).
        client._seq = max([client._seq] + [int(c["call_id"].rsplit("-", 1)[1]) for c in reusable.values()])
    jobs = []
    for p in pages:
        for view in decisions[p.page_id].views:
            old = reusable.get((view.view_id, tuple(view.crop)))
            if old is not None and old["options"] == options and view.kind == "whole" and old["sent_sha256"] == p.image_sha256:
                raw = load(resolve(old["raw_path"]))
                responses[view.view_id] = raw["result"]["layoutParsingResults"][0]["prunedResult"]
                reused.append(old | {"reused_from": str(reuse_dir)})
            else:
                jobs.append((p, view))
    ocr_started = time.perf_counter()
    if ocr_concurrency <= 1:            # 기본: OCR 전역 직렬(서버 추론 직렬)
        done = [client.call(p, view) for p, view in jobs]
    else:                               # 동시성 비교 실험용
        with ThreadPoolExecutor(max_workers=ocr_concurrency) as pool:
            done = list(pool.map(lambda pv: client.call(*pv), jobs))
    for (p, view), (_, pruned) in zip(jobs, done):
        if pruned is not None:
            responses[view.view_id] = pruned
    ocr_wall = round(time.perf_counter() - ocr_started, 3)
    calls = reused + sorted((asdict(c) for c in client.calls), key=lambda c: c["call_id"])
    dump(out / "calls.json", calls)
    meta = {"contract": CONTRACT, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "live", "manifests": [str(m) for m in manifests], "pdf_dpi": pdf_dpi,
            "pdf_dpi_note": "정답 좌표계(200DPI 렌더)와 비교하기 위한 조건. 200/300DPI 비교 전 확정값 아님.",
            "routing_policy": policy.to_dict(), "boundary_plans": sorted(plans),
            "ocr_options": options, "layout_options": layout_options,
            "layout_options_note": "비어 있으면 요청에 넣지 않음(서버 PP-StructureV3 설정의 class별 문턱값·중첩 처리).",
            "ocr_concurrency": ocr_concurrency, "ocr_wall_seconds": ocr_wall,
            "run_candidate_boundaries": run_candidates, "reused_ocr_from": str(reuse_dir) if reuse_dir else None,
            "reused_ocr_calls": len(reused), "new_ocr_calls": len(client.calls),
            "auto_boundary_pages": auto_plans, "auto_boundary_timing": auto_timing, "vlm_concurrency": vlm_concurrency,
            "assignment_rule": rule.to_dict(), "only_pages": sorted(only_pages or []),
            "page_ids": [p.page_id for p in pages]}
    dump(out / "pages.json", [p.to_dict() for p in pages])
    dump(out / "routes.json", [decisions[p.page_id].to_dict() for p in pages])
    result = _build_all(out, pages, decisions, calls, responses, rule, meta)
    meta["wall_seconds"] = round(time.perf_counter() - started, 3)
    dump(out / "run.json", meta)
    return result


def resolve(path: str) -> Path:
    """기록된 상대 경로는 프로젝트 루트 기준이다."""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def validate_layout_options(options: dict) -> dict:
    """PaddleX 3.6.1 서빙 스키마(InferRequest)와 DetPostProcess 에서 확인한 레이아웃 옵션만 허용한다."""
    allowed = {"layoutThreshold", "layoutNms", "layoutMergeBboxesMode", "layoutUnclipRatio"}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f"확인되지 않은 레이아웃 옵션: {sorted(unknown)}")
    mode = options.get("layoutMergeBboxesMode")
    if isinstance(mode, str) and mode not in ("union", "large", "small"):
        raise ValueError(f"layoutMergeBboxesMode 는 union/large/small 중 하나: {mode}")
    if "layoutNms" in options and not isinstance(options["layoutNms"], bool):
        raise ValueError("layoutNms 는 boolean")
    return dict(options)


def _build_all(out: Path, pages: list[Page], decisions: dict, calls: list[dict], responses: dict,
               rule: AssignmentRule, meta: dict) -> dict:
    from .report import build_report

    records = []
    for p in pages:
        page_calls = [c for c in calls if c["page_id"] == p.page_id]
        record = build_page(p, decisions[p.page_id], page_calls, responses, rule)
        dump(out / "records" / f"{p.page_id}.json", record)
        records.append(record)
    stage1 = {
        "contract": CONTRACT,
        "note": "영역 bbox 는 layout_det_res 원시 검출 그대로다. text 는 VLM 판독 전이라 null 이다. "
                "unassigned_texts 는 OCR 원문이며 최종 영역이 아니다.",
        "pages": [{"page_id": r["page"]["page_id"], "file_id": r["page"]["file_id"],
                   "submission_id": r["page"]["submission_id"], "width": r["page"]["width"],
                   "height": r["page"]["height"], "route": r["route"]["route"], "status": r["status"],
                   "input_views": r["route"]["views"], "ocr_calls": r["calls"]} for r in records],
        "regions": [{k: x[k] for k in ("region_id", "page_id", "view_id", "source", "source_index", "bbox",
                                       "table_candidate", "text", "text_status")} | {"table_verified": None}
                    for r in records for x in r["regions"]],
        "unassigned_texts": [u for r in records for u in r["unassigned_texts"]],
        "issues": [i | {"page_id": r["page"]["page_id"]} for r in records for i in r["issues"]],
    }
    dump(out / "stage1.json", stage1)
    report = build_report(records, calls, meta)
    dump(out / "report.json", report)
    (out / "report.md").write_text(report["markdown"], encoding="utf-8")
    return report
