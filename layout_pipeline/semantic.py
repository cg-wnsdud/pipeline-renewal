"""의미 단계 실행(docs/14): 저장된 파싱 결과(layout-result/2) → 분류·영역 배정(A~C) → 라벨링(D) → 검사(E) → layout-result/3·화면(F).

- OCR·전사(판독)는 호출하지 않는다. 입력은 <parse>/files/index.json 과 파일별 result.json 이며 읽기만 한다.
- 제출 묶음(submission)마다 따로 처리한다. 묶음 안에서는 관측(A, 페이지별이면 병렬) → 통합(B, 관측이 여럿일 때만) →
  영역 배정(C, 호출 없음) → 상품·영역 묶음별 라벨링(D, 병렬) 순서다. 라벨링은 상품·템플릿이 정해진 뒤에만 한다.
- GPU 동시 호출 상한은 파싱 단계의 --vlm-concurrency 와 같은 값 하나(Gate)를 의미 단계의 모든 호출이 함께 쓴다.
  단계·묶음마다 따로 상한을 두지 않는다(동시 호출 수가 단계 수만큼 불어나지 않게).
- 요청 메타(프롬프트·스키마·이미지 해시)와 원시 응답은 <out>/trace/semantic/calls 에, 별칭·근거 인용·특징 ID·검증 결과는
  <out>/trace/semantic/<submission>.json 에 남는다. 재구성(ReplayClient)은 같은 요청 열쇠의 저장 응답을 다시 써서 같은 결과를
  만든다(호출 없음). 요청 내용이 저장 때와 다르면 멈춘다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import classify, contract_v3, deposit_marks, handoff, labeling
from . import semantic_assets as A
from .file_result_v3 import CONTRACT, SEMANTIC_PROFILE, build_docs, write_docs
from .jsonio import dump, load
from .vlm_client import VlmCall, png_part


class Gate:
    """GPU 동시 호출 상한. 의미 단계의 모든 호출이 이 하나를 지난다. peak 는 실제 최대 동시 호출 수(검사용)."""

    def __init__(self, client, limit: int):
        self.client, self.limit = client, limit
        self._sem = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self.active = self.peak = self.calls = 0

    def chat(self, *args, **kwargs):
        with self._sem:
            with self._lock:
                self.active += 1
                self.calls += 1
                self.peak = max(self.peak, self.active)
            try:
                return self.client.chat(*args, **kwargs)
            finally:
                with self._lock:
                    self.active -= 1


class ReplayClient:
    """저장된 응답을 요청 열쇠(meta.key)로 돌려준다. 프롬프트·스키마·이미지가 저장 때와 다르면 ValueError."""

    replay = True

    def __init__(self, calls_dir: Path):
        self.records = {}
        for path in sorted(calls_dir.glob("*.json")):
            rec = load(path)
            self.records[rec["request_meta"]["key"]] = rec
        self.count = 0

    def chat(self, text, images, schema, name, max_tokens, meta=None):
        key = (meta or {}).get("key")
        rec = self.records.get(key)
        if rec is None:
            raise KeyError(f"저장 응답 없음: {key}")
        m = rec["request_meta"]
        if m["prompt"] != text or m["schema"] != schema:
            raise ValueError(f"{key}: 저장 때와 요청(프롬프트·스키마)이 다름")
        if [png_part(im)[1]["sha256"] for im in images] != [i["sha256"] for i in rec["images"]]:
            raise ValueError(f"{key}: 저장 때와 요청 이미지가 다름")
        self.count += 1
        return VlmCall(**{k: rec.get(k) for k in VlmCall.__dataclass_fields__})


def load_parse(parse_dir: Path) -> tuple[list[dict], dict[str, str]]:
    """<parse>/files/index.json 순서의 layout-result/2 문서와 경로."""
    idx = load(parse_dir / "files" / "index.json")
    docs, paths = [], {}
    for f in idx["files"]:
        path = parse_dir / f["result"]
        d = load(path)
        if d["contract"] != "layout-result/2":
            raise ValueError(f"{path}: layout-result/2 가 아님({d['contract']})")
        docs.append(d)
        paths[d["source"]["file_id"]] = str(path)
    return docs, paths


def _content_hash(call) -> str:
    return hashlib.sha256(json.dumps([call.content, call.error], ensure_ascii=False).encode("utf-8")).hexdigest()


# 상품 분류 모드(docs/16 §9): observe(고정 경로) = 관측 한 호출에서 상품·템플릿·모든 영역 소속을 함께 정하고(큰 묶음은 페이지별
# 관측 + 통합) 이름 없는 대상 정리(R1·R2)로 검증. legacy = observe 에서 정리 규칙만 뺀 이전 실행 재구성용.
CLASSIFICATION_MODES = ("observe", "legacy")
OBSERVE_PROFILE = "semantic-observe/1"


def semantic_versions(classification_mode: str = "legacy") -> dict:
    """결과·보고서에 남기는 요청 지시 버전과 자산 버전."""
    rules = {"target_rules": classify.TARGET_RULES_VERSION} if classification_mode == "observe" else {}
    return {"observe": classify.OBSERVE_VERSION, "region_order": classify.REGION_ORDER_VERSION,
            "mixed_check": classify.MIXED_CHECK_VERSION, **rules,
            "decisive": classify.DECISIVE_RULES_VERSION, "first_input": classify.FIRST_INPUT_VERSION,
            "integrate": classify.INTEGRATE_VERSION, "label": labeling.LABEL_VERSION,
            "template_request": classify.TEMPLATE_REQUEST_VERSION, "deposit_marks": deposit_marks.VERSION,
            **A.versions()}


class _Runner:
    def __init__(self, gate: Gate, out_dir: Path, record: bool, limit: int):
        self.gate, self.out_dir, self.record, self.limit = gate, out_dir, record, limit
        self.calls_dir = out_dir / "trace" / "semantic" / "calls"

    def send(self, req: dict, meta: dict) -> VlmCall:
        call = self.gate.chat(req["text"], req["images"], req["schema"], f"semantic_{meta['kind']}", req["max_tokens"],
                              meta | {"version": req["version"], "image_roles": req.get("image_roles")})
        if self.record:
            dump(self.calls_dir / f"{call.call_id}.json", asdict(call))
        return call

    def parallel(self, items: list[tuple[dict, dict]]) -> list[VlmCall]:
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(self.limit, len(items)))) as pool:
            return list(pool.map(lambda it: self.send(*it), items))


def aggregate_labels(b: dict, res: dict, jobs: list[dict], lres: list[dict], direct: dict) -> dict[str, dict]:
    """영역별 라벨 상태. 빈 label_ids 는 '라벨 없음' 판정(완료)일 때만 쓰고, 실패·미실행은 items 를 비우고 코드로 구별한다."""
    by = {(a, j["product"]["product_id"]): r for j, r in zip(jobs, lres) for a in j["regions"]}
    out = {}
    for a, reg in b["regions"].items():
        rid = reg["region_id"]
        x = res["assignments"][rid]
        rec = {"status": "not_run", "items": [], "codes": [], "evidence": {}, "rejected": {}, "calls": {}}
        if x["status"] == "completed":
            failed = False
            for pid in x["product_ids"]:
                why = direct.get((rid, pid))
                if why == "empty_text":
                    rec["items"].append({"product_id": pid, "label_ids": []})
                elif why:
                    rec["codes"].append(why)
                else:
                    r = by[(a, pid)]
                    rec["calls"][pid] = r["call"]
                    if not r["ok"] or a in r["missing"]:
                        rec["codes"].append("label_failed" if not r["ok"] else "label_missing")
                        failed = True
                        continue
                    g = r["regions"][a]
                    rec["items"].append({"product_id": pid, "label_ids": g["labels"]})
                    rec["evidence"][pid] = g["evidence"]
                    if g["rejected"]:
                        rec["rejected"][pid] = g["rejected"]
                        rec["codes"].append("label_quote_rejected")
            n = len(x["product_ids"])
            rec["status"] = "completed" if len(rec["items"]) == n else "partial" if rec["items"] else "failed" if failed else "not_run"
            rec["codes"] = list(dict.fromkeys(rec["codes"]))
        out[rid] = rec
    return out


def _usage(calls: list[VlmCall]) -> dict:
    u = Counter()
    for c in calls:
        for k in ("prompt_tokens", "completion_tokens"):
            u[k] += (c.usage or {}).get(k) or 0
    return {"calls": len(calls), "failed": sum(bool(c.error) for c in calls), "call_seconds": round(sum(c.seconds or 0 for c in calls), 3),
            **dict(u)}


def _ocr_texts(docs: list[dict], paths: dict[str, str]) -> dict[str, list[str]]:
    """stage1 기록(layout-result/2 trace_ref)의 영역별 배정 OCR 글자. 예금자보호 표시 후보를 찾는 데만 쓴다(모델 입력 아님).
    기록이 없으면(내보낸 파싱 결과만 있는 경우) 빈 값이고 후보는 최종 문구로만 찾는다."""
    out: dict[str, list[str]] = {}
    for d in docs:
        base = os.path.dirname(paths[d["source"]["file_id"]])
        for ref in (d.get("trace_ref") or {}).get("stage1_records") or []:
            path = Path(os.path.normpath(os.path.join(base, ref)))
            if not path.is_file():
                continue
            for a in load(path).get("assignments") or []:
                if a.get("region_id") and a.get("ocr_text"):
                    out.setdefault(a["region_id"], []).append(a["ocr_text"])
    return out


# 예금자보호 표시 확인 방식: run(확인) | disabled(LP_DEPOSIT_MARKS=off) | not_checked(이 기능 이전 실행의 재구성)
MARKS_MODES = ("run", "disabled", "not_checked")


def run_submission(sub: str, docs: list[dict], paths: dict[str, str], runner: _Runner,
                   classification_mode: str = "legacy", template_ids: list[str] | None = None, marks: str = "run") -> dict:
    out_dir = runner.out_dir
    t0 = time.perf_counter()
    timing = {}
    b = classify.build_bundle(docs, paths, template_ids)
    plan = classify.plan_units(b)
    whole = len(plan) == 1
    units = [{"unit": f"U{i + 1}", "pages": pages} for i, pages in enumerate(plan)]
    reqs = [classify.observe_request(b, u["pages"], whole) for u in units]
    # 예금자보호 표시 확인은 관측과 무관해 같은 동시 상한 안에서 관측과 함께 보낸다
    cands, mreqs = [], []
    if marks == "run":
        cands = deposit_marks.candidates(b, _ocr_texts(docs, paths))
        images: dict = {}
        mreqs = [(c, deposit_marks.request(b, c, images)) for c in cands[:deposit_marks.MAX_CANDIDATES]]
        images.clear()
    mitems = [(r, {"kind": "marks", "key": f"marks:{sub}:{c['region_id']}", "submission_id": sub, "region_id": c["region_id"]})
              for c, r in mreqs if r is not None]
    sent = runner.parallel([(r, {"kind": "observe", "key": f"observe:{sub}:{u['unit']}", "submission_id": sub, "pages": u["pages"]})
                            for u, r in zip(units, reqs)] + mitems)
    calls, mcalls = sent[:len(units)], sent[len(units):]
    keyed = [(f"observe:{sub}:{u['unit']}", c) for u, c in zip(units, calls)] + [(m["key"], c) for (_, m), c in zip(mitems, mcalls)]
    if marks == "run":
        it = iter(mcalls)
        mchecks = [{k: c[k] for k in ("region_id", "page_id", "bbox")} | deposit_marks.check(next(it) if r is not None else None)
                   for c, r in mreqs]
        marks_out = deposit_marks.summarize(mchecks, max(0, len(cands) - deposit_marks.MAX_CANDIDATES), deposit_marks.pages_complete(b))
    else:
        mchecks, marks_out = [], deposit_marks.not_checked("DISABLED" if marks == "disabled" else "NOT_CHECKED")
    timing["deposit_marks"] = {"candidates": len(cands), **_usage(mcalls)}
    for u, r, c in zip(units, reqs, calls):
        u["obs"] = classify.validate_observe(c, r, b)
        if classification_mode == "observe":       # 상품 ID·영역 연결 검증 뒤 이름 없는 대상 정리(R1·R2)
            u["obs"] = classify.apply_target_rules(u["obs"])
        u["call"] = c.call_id
    timing["observe"] = {"wall_seconds": round(time.perf_counter() - t0, 3), **_usage(calls)}
    integ, icalls = None, []
    t1 = time.perf_counter()
    if len(units) > 1:
        targets = classify.global_targets(units)
        if targets:
            r = classify.integrate_request(b, units, targets)
            c = runner.send(r, {"kind": "integrate", "key": f"integrate:{sub}", "submission_id": sub})
            integ = classify.validate_integrate(c, r) | {"call": c.call_id, "targets": targets}
            icalls = [c]
            keyed.append((f"integrate:{sub}", c))
        else:
            integ = {"ok": True, "errors": [], "products": [], "missing": [], "call": None, "targets": {}}
    timing["integrate"] = {"wall_seconds": round(time.perf_counter() - t1, 3), **_usage(icalls)}
    res = classify.resolve(b, units, integ)
    t2 = time.perf_counter()
    jobs, direct = labeling.plan_jobs(b, res["products"], res["assignments"])
    prods = {p["product_id"]: p for p in res["products"]}
    lreqs = [labeling.label_request(b, j, res["assignments"], prods) for j in jobs]
    lkeys = [f"label:{sub}:{j['product']['product_id']}:{j['index']}" for j in jobs]
    lcalls = runner.parallel([(r, {"kind": "label", "key": k, "submission_id": sub, "product_id": j["product"]["product_id"]})
                              for j, r, k in zip(jobs, lreqs, lkeys)])
    keyed += list(zip(lkeys, lcalls))
    lres = [labeling.validate_labels(c, r, b) | {"call": c.call_id} for c, r in zip(lcalls, lreqs)]
    labels = aggregate_labels(b, res, jobs, lres, direct)
    timing["label"] = {"wall_seconds": round(time.perf_counter() - t2, 3), **_usage(lcalls)}
    ok_units = sum(u["obs"]["ok"] for u in units)
    degraded = (ok_units < len(units) or (integ is not None and not integ["ok"]) or any(not r["ok"] or r["missing"] for r in lres)
                or any(u["obs"]["missing"] for u in units))
    status = "failed" if units and not ok_units else "partial" if degraded else "completed"
    profile = OBSERVE_PROFILE if classification_mode == "observe" else SEMANTIC_PROFILE
    trace_path = out_dir / "trace" / "semantic" / f"{sub}.json"
    docs3 = build_docs(out_dir, docs, paths, res["products"], res["assignments"], labels, status, semantic_versions(classification_mode),
                       [[k, _content_hash(c)] for k, c in keyed], trace_path, profile, classification_mode,
                       template_request=b["request"], deposit_insurance_marks=marks_out)
    timing["total_wall_seconds"] = round(time.perf_counter() - t0, 3)
    trace = {
        "submission_id": sub, "mode": "single" if whole else "paged", "status": status,
        "template_request": b["request"],
        "deposit_marks": {"mode": marks, "candidates": cands, "checked": mchecks, "result": marks_out},
        "aliases": {"files": {f["alias"]: f["file_id"] for f in b["files"]},
                    "pages": {pa: b["pages"][pa]["page_id"] for pa in b["page_order"]},
                    "regions": {a: r["region_id"] for a, r in b["regions"].items()}},
        "units": [{"unit": u["unit"], "pages": u["pages"], "call": u["call"]} | u["obs"] for u in units],
        "integration": integ,
        "products": res["products"], "groups": res["groups"], "merge_rules": res["merge_rules"], "assignments": res["assignments"],
        "labels": labels,
        "label_calls": [{"key": k, "call": r["call"], "product_id": j["product"]["product_id"], "regions": j["regions"],
                         "schemas": j["keys"], "ok": r["ok"], "errors": r["errors"], "missing": r["missing"]}
                        for k, j, r in zip(lkeys, jobs, lres)],
        "timing": timing}
    dump(trace_path, trace)
    return {"submission_id": sub, "docs": docs3, "trace": trace, "timing": timing, "bundle": b}


EXAMPLE_SOURCE_MIN_QUOTES = 3     # 같은 출처 광고의 서로 다른 예시 문구가 이만큼 들어 있으면 그 광고(개발·회귀 자료)로 본다


def example_overlap(docs: list[dict]) -> dict:
    """라벨 스키마의 실제 광고 예시 문구가 입력 영역 문구에 들어 있는 정도(개발·회귀 자료 구분용, 모델 입력 아님).
    웹 머리글처럼 여러 광고에 공통인 문구 한두 개는 흔하므로 출처 광고별 서로 다른 문구 수로 판단한다."""
    rows = {(classify.canon(r["quote"]), r.get("source_ad")) for k in A.LABEL_SCHEMA_KEYS
            for r in A.label_schema(k)["positives"] + A.label_schema(k)["negatives"] if len(classify.canon(r["quote"])) >= 12}
    out = {}
    for d in docs:
        text = classify.canon("\n".join(r["text"] or "" for r in d["regions"]))
        per_ad = Counter(ad for q, ad in rows if q in text)
        if per_ad:
            out[d["source"]["file_id"]] = {"matched_quotes_by_source_ad": dict(per_ad.most_common()),
                                           "dev_regression_sources": sorted(ad for ad, n in per_ad.items() if n >= EXAMPLE_SOURCE_MIN_QUOTES)}
    return out


def check_all(results: list[dict], docs2: dict[str, dict], paths2: dict[str, str], out_dir: Path) -> dict:
    """계약·참조·보존·라벨 인용 검사. 내용(분류·라벨) 정확도 검사가 아니다."""
    errors, preservation, evidence = {}, {}, []
    all_docs = [d for r in results for d in r["docs"]]
    for r in results:
        regions = {x["region_id"]: x for d in r["docs"] for x in d["regions"]}
        for d in r["docs"]:
            fid = d["source"]["file_id"]
            errors[fid] = contract_v3.check_document(d)
            preservation[fid] = contract_v3.check_preservation(d, out_dir / "files" / fid / "result.json", docs2[fid], paths2[fid])
        for rid, lb in r["trace"]["labels"].items():
            for it in lb["items"]:
                for label in it["label_ids"]:
                    ev = [e for e in lb["evidence"].get(it["product_id"], []) if e["label"] == label]
                    if not ev or classify.quote_where(ev[0]["quote"], regions[rid]) is None:
                        evidence.append(f"{rid}: {it['product_id']} {label} 인용이 영역 문구·표 셀에 없음")
    sub_errors = contract_v3.check_submission(all_docs)
    return {"passed": not any(errors.values()) and not any(preservation.values()) and not sub_errors and not evidence,
            "contract_errors": {k: v for k, v in errors.items() if v},
            "preservation_errors": {k: v for k, v in preservation.items() if v},
            "submission_errors": sub_errors, "label_evidence_errors": evidence,
            "documents": len(all_docs),
            "statement": "구조·참조·보존·인용 위치 검사다. 상품 분류·템플릿·라벨 내용의 정확도는 검사하지 않는다."}


def run(parse_dir: Path, out_dir: Path, client, concurrency: int, submissions: set[str] | None = None,
        replay: bool = False, classification_mode: str = "observe", template_requests: dict[str, list[str]] | None = None,
        marks: str | None = None) -> dict:
    """의미 단계 전체 실행(또는 replay=True 이면 저장 응답으로 재구성). 결과 요약을 돌려준다.
    template_requests: 묶음별 등록 템플릿 ID(없는 묶음은 자동 분류). marks: 예금자보호 표시 확인(MARKS_MODES, 기본 LP_DEPOSIT_MARKS)."""
    from .final_viewer import build_semantic_viewer
    from .report_html import build_report
    if classification_mode not in CLASSIFICATION_MODES:
        raise ValueError(f"알 수 없는 상품 분류 모드: {classification_mode}")
    marks = marks or ("run" if deposit_marks.enabled() else "disabled")
    if marks not in MARKS_MODES:
        raise ValueError(f"알 수 없는 예금자보호 표시 확인 방식: {marks}")
    requests = {k: list(v) for k, v in (template_requests or {}).items() if v}
    for ids in requests.values():
        classify.template_request(ids)          # 모르는 템플릿이면 호출 전에 멈춘다
    profile = OBSERVE_PROFILE if classification_mode == "observe" else SEMANTIC_PROFILE
    t0 = time.perf_counter()
    docs, paths = load_parse(parse_dir)
    by_sub: dict[str, list[dict]] = {}
    for d in docs:
        by_sub.setdefault(d["submission_id"], []).append(d)
    subs = [s for s in by_sub if not submissions or s in submissions]
    gate = Gate(client, concurrency)
    runner = _Runner(gate, out_dir, record=not replay, limit=concurrency)
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(subs)))) as pool:
        results = list(pool.map(lambda s: run_submission(s, by_sub[s], paths, runner, classification_mode,
                                                         requests.get(s), marks), subs))
    docs2 = {d["source"]["file_id"]: d for d in docs}
    checks = check_all(results, docs2, paths, out_dir)
    all_docs = [d for r in results for d in r["docs"]]
    index = write_docs(out_dir, all_docs)
    dump(out_dir / "files" / "index.json", {"contract": CONTRACT + "/index", "files": index})
    dump(out_dir / "files" / "contract-check.json", checks)
    # 심의 단계 입력(review-input/2, docs/21): 묶음마다 필요한 값만 추린 JSON(<out>/handoff/<submission_id>.json)
    hand = handoff.write_all(out_dir)
    viewer = build_semantic_viewer(out_dir, all_docs, [r["trace"] for r in results], checks)
    timing = {"wall_seconds": round(time.perf_counter() - t0, 3), "concurrency_limit": concurrency, "peak_concurrency": gate.peak,
              "http_calls": gate.calls, "replay": replay}
    report = {
        "contract": CONTRACT, "semantic_profile": profile, "versions": semantic_versions(classification_mode), "parse_dir": str(parse_dir),
        "timing": timing, "checks": {k: checks[k] for k in ("passed", "documents")} | {
            "problems": sum(len(v) for v in checks["contract_errors"].values()) + sum(len(v) for v in checks["preservation_errors"].values())
            + len(checks["submission_errors"]) + len(checks["label_evidence_errors"])},
        "dev_regression_overlap": example_overlap([d for r in results for d in by_sub[r["submission_id"]]]),
        "submissions": [summary(r) for r in results], "viewer": str(viewer),
        "handoff": {"contract": handoff.CONTRACT, "passed": not any(v["problems"] for v in hand.values()),
                    "files": {k: v["path"] for k, v in hand.items()}}}
    # 재구성은 실행 당시 보고서(호출 수·시간)를 덮어쓰지 않는다
    dump(out_dir / ("report.rebuild.json" if replay else "report.json"), report)
    if not replay:
        dump(out_dir / "run.json", {"parse_dir": str(parse_dir), "submissions": subs, "concurrency": concurrency,
                                    "semantic_profile": profile, "versions": semantic_versions(classification_mode),
                                    "classification_mode": classification_mode, "template_requests": requests,
                                    "deposit_marks": marks, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    report["report_html"] = str(build_report(out_dir))     # 시간은 원본 report.json 에서 읽는다(재구성도 그 파일을 덮어쓰지 않음)
    return report


def summary(r: dict) -> dict:
    """묶음별 요약(보고서): 상품·템플릿·라벨 분포·상태 수·호출·시간."""
    regions = [x for d in r["docs"] for x in d["regions"]]
    prods = {p["product_id"]: p for d in r["docs"] for p in d["products"]}
    labels = Counter()
    for x in regions:
        for it in x["labeling"]["items"]:
            labels.update(f"{it['product_id']}:{lab}" for lab in it["label_ids"])
    tr = r["trace"]
    classification = {"units": len(tr["units"]),
                      "target_rules": [a for u in tr["units"] for a in (u.get("target_rules") or [])]}
    return {"submission_id": r["submission_id"], "mode": tr["mode"], "status": tr["status"],
            "files": [d["source"]["file_id"] for d in r["docs"]], "pages": sum(len(d["pages"]) for d in r["docs"]),
            "regions": len(regions), "classification": classification,
            "products": [{"product_id": p["product_id"], "name": p["name"], "group": p["product_group"], "status": p["status"],
                          "templates": [t["template_id"] for t in p["templates"]], "pension": p["pension"], "files": p["files"],
                          "review": p["review"]["codes"]} for p in prods.values()],
            "assignment": dict(Counter(f"{x['product_assignment']['status']}:{x['product_assignment']['scope']}" for x in regions)),
            "labeling": dict(Counter(x["labeling"]["status"] for x in regions)),
            "labels": dict(sorted(labels.items())),
            "regions_with_multiple_labels": sum(any(len(it["label_ids"]) > 1 for it in x["labeling"]["items"]) for x in regions),
            "semantic_review": dict(Counter(c for x in regions for c in x["semantic_review"]["codes"])),
            "timing": r["timing"]}


def rebuild(out_dir: Path) -> dict:
    """저장된 의미 단계 응답으로 결과·화면·보고서를 다시 만든다(모델 호출 없음)."""
    info = load(out_dir / "run.json")
    client = ReplayClient(out_dir / "trace" / "semantic" / "calls")
    marks = info.get("deposit_marks")          # 이 기능 이전 실행은 기록이 없다(확인하지 않은 것으로 재구성)
    return run(Path(info["parse_dir"]), out_dir, client, info["concurrency"], set(info["submissions"]), replay=True,
               classification_mode=info.get("classification_mode", "legacy"), template_requests=info.get("template_requests"),
               marks=marks if marks in MARKS_MODES else "not_checked")
