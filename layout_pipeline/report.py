"""실행 보고서. 서로 다른 판정을 섞지 않는다.

- 실행 완료: 호출·빌드가 끝났는가(READY 등). 내용 품질과 무관.
- 보존 계약: 원시 레이아웃 상자 = Region, 원시 OCR = 배정 + 미배정(정확한 분할).
- 영역 검출(고정 기하 기준): 정답 줄이 원시 레이아웃 bbox 에 미포함/일부/단일/복수 포함.
- 배정 정책: 현재 배정 규칙으로 단일 영역에 배정된 비율(검출 재현율 아님).
- 원문 품질: VLM 최종 문구 정확도. 이 보고서 단계에서는 미측정.

집계 범위(docs/11 §5): 실행 완료 = READY(확정 경로) + CANDIDATE_RUN(미검증 후보 경계로 실행 완료). 보존 대조의 원시 분모는
최종 영역을 만든 OCR 호출(whole·tile·candidate_tile)만 쓴다. 긴 이미지 경계 탐색(scout) 호출은 따로 센다.
평가 정답이 없는 페이지는 영역 검출을 '미측정'으로 둔다.
"""
from __future__ import annotations

from collections import Counter

AMBIGUOUS = ("ambiguous_multiple_regions", "ambiguous_overlapping_regions")
EXECUTED = ("READY", "CANDIDATE_RUN")
SCOUT_KIND = "scout"


def _page_row(r: dict, calls: list[dict]) -> dict:
    page, route = r["page"], r["route"]
    page_calls = [c for c in calls if c["page_id"] == page["page_id"] and c.get("view_kind") != SCOUT_KIND]
    scout_calls = [c for c in calls if c["page_id"] == page["page_id"] and c.get("view_kind") == SCOUT_KIND]
    whole = [c for c in page_calls if c["view_kind"] == "whole"]
    nonempty_un = [u for u in r["unassigned_texts"] if not u["empty_text"]]
    assigned = r["assignments"]
    gold = r.get("gold") or {}
    overlaps = r["diagnostics"]["region_overlaps"]
    return {
        "page_id": page["page_id"], "kind": page["source"]["kind"], "size": [page["width"], page["height"]],
        "aspect": page["geometry"]["aspect"], "orientation": page["geometry"]["orientation"],
        "shape": route["shape"], "route": route["route"], "status": r["status"],
        "quality": r.get("quality", {}).get("status"),
        "ocr_calls": len(page_calls), "ocr_call_ids": [c["call_id"] for c in page_calls],
        "ocr_seconds": [c["seconds"] for c in page_calls],
        "scout_calls": len(scout_calls), "scout_seconds": [c["seconds"] for c in scout_calls],
        "whole_input_is_full_page": bool(whole) and all(
            c["input_crop"] == [0, 0, page["width"], page["height"]] and c["sent_sha256"] == page["image_sha256"]
            and c["response_size_matches"] for c in whole),
        "regions": len(r["regions"]), "table_candidates": sum(x["table_candidate"] for x in r["regions"]),
        "server_blocks": len(r["server_blocks"]),
        "ocr_items": r["ocr_items_total"],
        "ocr_nonempty": sum(1 for a in assigned if not a["empty_text"]) + len(nonempty_un),
        "assigned_nonempty": sum(1 for a in assigned if not a["empty_text"]),
        "assigned_exact": sum(1 for a in assigned if not a["empty_text"] and a["exact_inside"]),
        "assigned_with_tolerance": sum(1 for a in assigned if not a["empty_text"] and not a["exact_inside"]),
        "unassigned_nonempty": dict(Counter(u["reason"] for u in nonempty_un)),
        "empty_text_items": sum(1 for a in assigned if a["empty_text"]) + sum(1 for u in r["unassigned_texts"] if u["empty_text"]),
        "partition_passed": r["partition_checks"]["passed"],
        "region_overlap_pairs": len(overlaps),
        "region_contained_pairs": sum(o["relation"] == "contained" for o in overlaps),
        "regions_without_text": len(r["diagnostics"]["regions_without_text"]),
        "gold_evaluated": bool(gold.get("evaluated")),
        "gold_required_lines": gold.get("required_lines"),
        "gold_geometry": (gold.get("geometry") or {}).get("counts"),
        "gold_geometry_partial": (gold.get("geometry") or {}).get("partial_inside_ratio"),
        "gold_policy": (gold.get("assignment_policy") or {}).get("counts"),
        "gold_tables": [(t["gold_table"], t["status"], t["best_fraction"]) for t in (gold.get("tables") or {}).get("gold_tables", [])],
        "table_candidates_not_in_gold_tables": len((gold.get("tables") or {}).get("candidates_not_in_gold_tables", [])),
        "vlm_plan": {"read_text": r["vlm_plan"]["checks"]["read_text"], "table_check": r["vlm_plan"]["checks"]["table_check"]},
        "issues": [i["code"] for i in r["issues"]],
    }


def build_report(records: list[dict], calls: list[dict], meta: dict) -> dict:
    rows = [_page_row(r, calls) for r in records]
    regress = []
    for row in rows:
        if row["shape"] in ("a4", "square"):
            ok = row["route"] == "whole" and row["ocr_calls"] == 1 and row["whole_input_is_full_page"]
            regress.append({"page_id": row["page_id"], "shape": row["shape"], "ocr_calls": row["ocr_calls"],
                            "call_ids": row["ocr_call_ids"], "passed": ok})
    ready = [r for r in rows if r["status"] in EXECUTED]
    evaluated = [r for r in ready if r["gold_evaluated"]]
    final_calls = [c for c in calls if c.get("view_kind") != SCOUT_KIND]
    scout_calls = [c for c in calls if c.get("view_kind") == SCOUT_KIND]
    totals = {
        "pages": len(rows), "ready_pages": sum(r["status"] == "READY" for r in rows),
        "candidate_run_pages": sum(r["status"] == "CANDIDATE_RUN" for r in rows), "executed_pages": len(ready),
        "pending_pages": [(r["page_id"], r["status"]) for r in rows if r["status"] not in EXECUTED],
        "ocr_calls": len(calls), "ocr_calls_ok": sum(c["ok"] for c in calls),
        "final_ocr_calls": len(final_calls), "scout_ocr_calls": len(scout_calls),
        "regions": sum(r["regions"] for r in rows), "table_candidates": sum(r["table_candidates"] for r in rows),
        "ocr_nonempty": sum(r["ocr_nonempty"] for r in ready),
        "assigned_nonempty": sum(r["assigned_nonempty"] for r in ready),
        "assigned_with_tolerance": sum(r["assigned_with_tolerance"] for r in ready),
        "unassigned_nonempty": dict(sum((Counter(r["unassigned_nonempty"]) for r in ready), Counter())),
        "partition_passed_all": all(r["partition_passed"] for r in ready),
        "gold_required_lines": sum(r["gold_required_lines"] or 0 for r in evaluated),
        "gold_geometry": dict(sum((Counter(r["gold_geometry"] or {}) for r in evaluated), Counter())),
        "gold_geometry_partial": dict(sum((Counter(r["gold_geometry_partial"] or {}) for r in evaluated), Counter())),
        "gold_policy": dict(sum((Counter(r["gold_policy"] or {}) for r in evaluated), Counter())),
        "a4_square_regression_passed": all(x["passed"] for x in regress),
    }
    totals["gold_covered"] = totals["gold_policy"].get("covered", 0)
    ok_calls = [c for c in final_calls if c["ok"]]
    totals["scout_raw"] = {"calls": len(scout_calls),
                           "layout_det_boxes": sum(c["counts"]["layout_det_boxes"] for c in scout_calls if c["ok"]),
                           "ocr_items": sum(c["counts"]["ocr_items"] for c in scout_calls if c["ok"]),
                           "note": "긴 이미지 경계 탐색용 응답. 최종 영역·배정 분모가 아니다."}
    rt = {
        "raw_layout_det_boxes": sum(c["counts"]["layout_det_boxes"] for c in ok_calls),
        "output_regions": sum(len(r["regions"]) for r in records),
        "raw_ocr_items": sum(c["counts"]["ocr_items"] for c in ok_calls),
        "output_assigned_plus_unassigned": sum(len(r["assignments"]) + len(r["unassigned_texts"]) for r in records),
    }
    rt["match"] = (rt["raw_layout_det_boxes"] == rt["output_regions"]
                   and rt["raw_ocr_items"] == rt["output_assigned_plus_unassigned"])
    totals["raw_to_output"] = rt
    n_cand = totals["candidate_run_pages"]
    status_table = [
        {"stage": "실행(OCR·빌드)", "state": f"{len(ready)}/{len(rows)} 페이지 실행 완료(READY {totals['ready_pages']}"
         + (f", CANDIDATE_RUN {n_cand}: 후보 경계 실행 완료·경계 미검증" if n_cand else "") + ")"
         + (f", 대기 {len(totals['pending_pages'])}" if totals["pending_pages"] else ""),
         "note": "실행 완료는 내용 품질 통과가 아니다. CANDIDATE_RUN 은 확정 결과가 아니다."},
        {"stage": "보존 계약", "state": "통과" if rt["match"] and totals["partition_passed_all"] else "실패",
         "note": "원시 레이아웃 상자 = Region, 원시 OCR = 배정 + 미배정."},
        {"stage": "영역 검출(고정 기하 기준)", "state": "미측정(평가 정답 없음)" if not totals["gold_required_lines"]
         else ("미통과" if totals["gold_geometry"].get("single", 0) < totals["gold_required_lines"] else "통과"),
         "note": "정답 줄이 원시 레이아웃 bbox 하나에 완전히 들어가는지."},
        {"stage": "원문 품질(VLM 최종 문구)", "state": "미측정", "note": "이 보고서는 Stage 1 산출물이다. VLM 평가는 별도 실행."},
    ]
    report = {"meta": meta, "status_table": status_table, "totals": totals, "a4_square_whole_once": regress,
              "pages": rows,
              "final_text_accuracy": {"measured": False, "reason": "Stage 1 보고서. VLM 판독 평가는 별도 실행 결과를 본다."}}
    report["markdown"] = _markdown(report)
    return report


def _pct(n, d) -> str:
    return f"{n}/{d} ({n / d:.1%})" if d else "-"


def _markdown(rep: dict) -> str:
    t, rows, meta = rep["totals"], rep["pages"], rep["meta"]
    rule = meta["assignment_rule"]
    lines = ["# Stage 1 보고 — 입력 · 레이아웃 영역 · OCR 배정", "",
             f"- 실행: {meta.get('created_at')} / 모드 {meta.get('mode')}"
             + (f" / 재빌드 {meta['rebuilt_at']}" if meta.get('rebuilt_at') else ""),
             f"- 레이아웃 요청 옵션: {meta.get('layout_options') or '없음(서버 기본)'}",
             f"- 경로 분기: long_aspect_min={meta['routing_policy']['long_aspect_min']} [{meta['routing_policy']['status']}]",
             f"- 배정 규칙: {rule['name']} core_shrink={rule['core_shrink']} (strict={rule.get('strict')}, {rule.get('status')})",
             f"- OCR 호출 {t['ocr_calls']}회(성공 {t['ocr_calls_ok']}) = 최종 영역용 {t['final_ocr_calls']} + 경계 탐색 {t['scout_ocr_calls']}, "
             f"페이지 {t['pages']}개 중 실행 완료 {t['executed_pages']}개(READY {t['ready_pages']}, CANDIDATE_RUN {t['candidate_run_pages']})",
             "", "## 0. 단계별 상태", "", "| 단계 | 상태 | 의미 |", "|---|---|---|"]
    for s in rep["status_table"]:
        lines.append(f"| {s['stage']} | {s['state']} | {s['note']} |")
    lines += ["", "## 1. A4·정사각형 페이지의 OCR 호출(호출 기록 기준)", "",
              "| page | 형태 | OCR 호출 수 | call_id | 판정 |", "|---|---|---:|---|---|"]
    for x in rep["a4_square_whole_once"]:
        lines.append(f"| {x['page_id']} | {x['shape']} | {x['ocr_calls']} | {', '.join(x['call_ids'])} | "
                     f"{'통과' if x['passed'] else '실패'} |")
    lines += ["", "판정 = route=whole, 호출 1회, 보낸 이미지 해시=페이지 해시, 입력 범위=페이지 전체, 응답 크기 일치.", "",
              "## 2. 페이지별 경로와 입력", "",
              "| page | 종류 | 크기 | 비율 | 형태 | 경로 | 실행 상태 | 품질 상태 | OCR 호출(최종 영역) | 초 | 경계 탐색 호출 |",
              "|---|---|---|---:|---|---|---|---|---:|---|---:|"]
    for r in rows:
        lines.append(f"| {r['page_id']} | {r['kind']} | {r['size'][0]}×{r['size'][1]} | {r['aspect']} | {r['shape']} | "
                     f"{r['route']} | {r['status']} | {r['quality']} | {r['ocr_calls']} | "
                     f"{', '.join(str(s) for s in r['ocr_seconds'])} | {r['scout_calls']} |")
    g, gp = t["gold_geometry"], t["gold_geometry_partial"]
    lines += ["", "## 3. 영역 검출 — 고정 기하 기준(exact-containment/1)", "",
              "정답 줄 bbox 가 원시 레이아웃 bbox 에 허용 오차 없이 완전히 들어가는 영역 수. 배정 규칙과 무관하고 설정 비교 내내 고정.",
              "partial 은 포함 면적비 구간을 함께 적는다(정답 bbox 의 OCR 여백 때문에 0.95 이상 구간이 생길 수 있음, 재분류하지 않음).", "",
              "| page | 영역 | 표 후보 | 정답 줄 | 미포함 | 일부(≥0.95 / 0.5~0.95 / <0.5) | 단일 | 복수 | 원시 중첩 쌍(포함) | 정답 표 |",
              "|---|---:|---:|---:|---:|---|---:|---:|---|---|"]
    for r in rows:
        if r["status"] not in EXECUTED:
            continue
        if not r["gold_evaluated"]:
            lines.append(f"| {r['page_id']} | {r['regions']} | {r['table_candidates']} | 평가 불가 | | | | | | |")
            continue
        c, p = r["gold_geometry"] or {}, r["gold_geometry_partial"] or {}
        tables = "; ".join(f"{a}:{b}({x:.0%})" for a, b, x in r["gold_tables"]) or "-"
        lines.append(f"| {r['page_id']} | {r['regions']} | {r['table_candidates']} | {r['gold_required_lines']} | "
                     f"{c.get('none', 0)} | {c.get('partial', 0)} ({p.get('ge_0.95', 0)}/{p.get('0.5_to_0.95', 0)}/{p.get('lt_0.5', 0)}) | "
                     f"{c.get('single', 0)} | {c.get('multiple', 0)} | {r['region_overlap_pairs']}({r['region_contained_pairs']}) | {tables} |")
    lines += ["", f"합계(정답 줄 {t['gold_required_lines']}): 미포함 {g.get('none', 0)}, 일부 {g.get('partial', 0)} "
              f"(≥0.95 {gp.get('ge_0.95', 0)} / 0.5~0.95 {gp.get('0.5_to_0.95', 0)} / <0.5 {gp.get('lt_0.5', 0)}), "
              f"단일 {g.get('single', 0)}, 복수 {g.get('multiple', 0)}.", "",
              f"## 4. 배정 정책 — {rule['name']} core_shrink={rule['core_shrink']} ({rule.get('status')})", "",
              "같은 배정 규칙을 정답 줄에 적용한 결과. 검출 재현율이 아니라 배정 정책의 단일 영역 배정률이다.", "",
              "| page | 정답 줄 | 단일 배정 | 모호 | 일부 | 여백만 걸침 | 영역 밖 |", "|---|---:|---|---:|---:|---:|---:|"]
    for r in rows:
        if r["status"] not in EXECUTED or not r["gold_evaluated"]:
            continue
        c = r["gold_policy"] or {}
        lines.append(f"| {r['page_id']} | {r['gold_required_lines']} | {_pct(c.get('covered', 0), r['gold_required_lines'])} | "
                     f"{sum(c.get(k, 0) for k in AMBIGUOUS)} | {c.get('partial_overlap', 0)} | "
                     f"{c.get('margin_overlap_only', 0)} | {c.get('outside_layout', 0)} |")
    gpol = t["gold_policy"]
    lines += ["", f"합계: 단일 배정 {_pct(gpol.get('covered', 0), t['gold_required_lines'])}, "
              f"모호 {sum(gpol.get(k, 0) for k in AMBIGUOUS)}, 일부 {gpol.get('partial_overlap', 0)}, "
              f"여백만 걸침 {gpol.get('margin_overlap_only', 0)}, 영역 밖 {gpol.get('outside_layout', 0)}.", "",
              "## 5. OCR 원시 항목 보존(배정/미배정 분할)", "",
              "모든 OCR 항목이 배정 또는 미배정 목록 중 정확히 한 곳에 있는지 검증한 결과. 글자가 맞는지는 별개다.", "",
              "| page | OCR(글자 있음) | 배정(정확 포함 + 허용 오차) | 미배정: 밖 | 여백만 | 부분 | 모호 | 분할 검증 |",
              "|---|---:|---|---:|---:|---:|---:|---|"]
    for r in rows:
        if r["status"] not in EXECUTED:
            continue
        u = r["unassigned_nonempty"]
        lines.append(f"| {r['page_id']} | {r['ocr_nonempty']} | {r['assigned_nonempty']} ({r['assigned_exact']} + "
                     f"{r['assigned_with_tolerance']}) | {u.get('outside_layout', 0)} | {u.get('margin_overlap_only', 0)} | "
                     f"{u.get('partial_overlap', 0)} | {sum(u.get(k, 0) for k in AMBIGUOUS)} | "
                     f"{'통과' if r['partition_passed'] else '실패'} |")
    un, rt = t["unassigned_nonempty"], t["raw_to_output"]
    lines += ["", f"합계: 글자 있는 OCR {t['ocr_nonempty']}개 중 배정 {t['assigned_nonempty']}개"
              f"(허용 오차 배정 {t['assigned_with_tolerance']}개), 미배정 {sum(un.values())}개 {dict(un)}. "
              f"분할 검증 전부 통과: {t['partition_passed_all']}.", "",
              f"원시→출력 대조(최종 영역용 OCR 호출 {t['final_ocr_calls']}회): layout_det 상자 {rt['raw_layout_det_boxes']} = "
              f"Region {rt['output_regions']}, OCR 항목 {rt['raw_ocr_items']} = 배정+미배정 {rt['output_assigned_plus_unassigned']} → "
              f"{'일치' if rt['match'] else '불일치'}."
              + (f" 경계 탐색 응답(호출 {t['scout_raw']['calls']}회, layout 상자 {t['scout_raw']['layout_det_boxes']}, "
                 f"OCR 항목 {t['scout_raw']['ocr_items']})은 탐색용이며 이 대조의 분모가 아니다." if t["scout_raw"]["calls"] else ""), "",
              "## 6. 원문 품질(VLM 최종 문구)", "", "이 보고서에서는 미측정. VLM 평가 실행 결과(work/vlm/*)를 별도로 본다.", "",
              "## 7. 처리하지 않은 페이지(READY·CANDIDATE_RUN 외)", ""]
    for pid, status in t["pending_pages"]:
        lines.append(f"- {pid}: {status}")
    if not t["pending_pages"]:
        lines.append("- 없음")
    return "\n".join(lines) + "\n"
