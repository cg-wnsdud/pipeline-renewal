"""판독 단계: Stage 1 페이지 기록의 모든 Region 을 VLM 으로 읽고 Region 단위 결과로 통합한다.

- 대상은 Stage 1 결과에서 자동 선정한다(고정 평가 ID 목록에 의존하지 않음).
  표 후보(table_candidate) → 표 확인 요청(table-check/3, crop 1장). 나머지 → 일반 영역 crop 묶음 요청.
- Region 은 논리 결과 단위다. 일반 영역은 페이지별 읽기 순서로 최대 max_images 장씩 한 요청에 묶는다.
- 실패 처리(모든 요청에 같은 규칙): 묶음 요청이 계약 수준에서 통째로 실패하면(HTTP·잘림·JSON·ID 집합) 그 묶음의
  영역만 한 번씩 단독 요청한다. 단독 요청도 실패하면 text=null(read_failed)로 남긴다. 표 요청은 재시도하지 않는다.
  사례별 재크롭·재시도 분기는 두지 않는다.
- 실패한 영역도 bbox·실패 사유와 함께 결과에 남는다. 표 판독 실패는 is_table 을 false 로 바꾸지 않고 unknown 이다.
- 표 판독 경로(table_read): legacy = table-check/3(crop)·page/2 tables(page-first). cells = table-cells/1(docs/11 §2):
  두 읽기 모드 모두 표 후보마다 text·셀 구조를 같은 응답으로 받아 하나의 후보로 쓴다(다른 판독의 text·rows 를 섞지 않음).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from . import page_read, table_cells, vlm_tasks
from .jsonio import dump

GENERAL_MAX_TOKENS = 8000
TABLE_MAX_TOKENS = 6000
# 판독 대상 실행 상태. CANDIDATE_RUN 은 미검증 후보 경계로 실험 실행한 긴 이미지(확정 결과 아님)다.
READABLE_STATUSES = ("READY", "CANDIDATE_RUN")


def reading_order(regions: list[dict]) -> list[dict]:
    return sorted(regions, key=lambda r: (round(r["bbox"][1] / 20), r["bbox"][0]))


def plan_page(record: dict, max_images: int, margin_px: int) -> list[dict]:
    """페이지의 판독 작업 목록. 각 작업 = HTTP 요청 1회 계획."""
    regions = record["regions"]
    general = reading_order([r for r in regions if not r["table_candidate"]])
    tables = [r for r in regions if r["table_candidate"]]
    jobs = [{"kind": "general", "regions": general[i:i + max_images], "margin_px": margin_px}
            for i in range(0, len(general), max_images)]
    jobs += [{"kind": "table", "regions": [r]} for r in tables]
    return jobs


class Reader:
    def __init__(self, client, trace_dir: Path, concurrency: int = 1, general_version: str = "crop/1",
                 table_version: str = vlm_tasks.TABLE_REQUEST_VERSION):
        self.client, self.trace_dir, self.concurrency = client, trace_dir, concurrency
        self.general_version, self.table_version = general_version, table_version
        self.resume: dict[str, tuple] = {}
        # 페이지 우선 판독의 영역 확인 방식: reread(region-reread/1, docs/10 채택) | patch(region-patch/1, 이전 실행 재현용)
        self.verify_mode = "reread"
        # 표 판독 경로: legacy(기본, 재현용) | cells(table-cells/1, docs/11 §2)
        self.table_read = "legacy"

    def load_resume(self) -> int:
        """중단된 실행의 페이지 판독 응답(trace/vlm, kind=page)을 다시 쓴다. 같은 페이지 요청을 반복 호출하지 않기 위함.
        새 호출 번호는 기존 기록 다음부터 이어 쓴다. 영역 확인 호출은 다시 계산해 보낸다."""
        from types import SimpleNamespace
        from .jsonio import load
        last = 0
        for path in sorted((self.trace_dir / "vlm").glob("*.json")):
            rec = load(path)
            last = max(last, int(rec["call_id"].rsplit("-", 1)[1]))
            meta = rec["request_meta"]
            if meta.get("kind") == "page":
                parsed = page_read.validate_page(SimpleNamespace(error=rec["error"], parsed=rec["parsed"]),
                                                 {"region_ids": meta["region_ids"], "table_ids": meta.get("table_ids") or []})
                self.resume[meta["view_id"]] = (rec, parsed)
            else:
                path.rename(path.with_suffix(".superseded"))   # 재개 시 영역 확인은 새로 계산한다(기록은 보존)
        self.client.count = last
        return len(self.resume)

    def _call(self, record: dict, img: Image.Image, regions: list[dict], kind: str, margin_px: int,
              attempt: str) -> tuple[dict, dict]:
        if kind == "table" and self.table_read == "cells":
            view = table_cells.context_view_for(record, regions[0])
            req = table_cells.request(record, regions[0], img, view)
            return self._send(record, req, "table_cells", table_cells.MAX_TOKENS, {"attempt": attempt, "context_view": view})
        if kind == "table":
            req = vlm_tasks.table_request(record, regions[0], img, self.table_version)
            max_tokens = TABLE_MAX_TOKENS
        else:
            req = vlm_tasks.crop_request(record, regions, img, margin_px, self.general_version)
            max_tokens = GENERAL_MAX_TOKENS
        call = self.client.chat(req["text"], req["images"], req["schema"], f"{kind}_read", max_tokens,
                                {"kind": kind, "page_id": record["page"]["page_id"], "region_ids": req["region_ids"],
                                 "version": req.get("version"), "margin_px": req.get("margin_px"),
                                 "items": req.get("items"), "attempt": attempt})
        rec = asdict(call)
        dump(self.trace_dir / "vlm" / f"{call.call_id}.json", rec)
        parsed = vlm_tasks.validate(call, req)
        return rec, parsed

    def run_job(self, record: dict, img: Image.Image, job: dict) -> dict[str, dict]:
        """작업 하나를 실행하고 region_id → 판독 결과(+trace)를 돌려준다."""
        regions, kind = job["regions"], job["kind"]
        rec, parsed = self._call(record, img, regions, kind, job.get("margin_px", 0), "batch" if len(regions) > 1 else "single")
        out = {}
        whole_failed = kind == "general" and len(regions) > 1 and all(not p["contract_ok"] for p in parsed.values())
        cells = rec["request_meta"]["kind"] == "table_cells"
        for r in regions:
            res = parsed[r["region_id"]]
            if cells and not res["contract_ok"]:
                res = res | {"table_structure_failed": True}       # 재구성(merge_entries) 경로와 같은 표시
            out[r["region_id"]] = res | {"trace": {"calls": [rec["call_id"]], "kinds": [rec["request_meta"]["kind"]],
                                                   "seconds": rec["seconds"], "finish_reason": rec["finish_reason"]}
                                         | ({"table_read": rec["request_meta"].get("version")} if cells else {})}
        if whole_failed:
            for r in regions:
                rec2, parsed2 = self._call(record, img, [r], kind, job.get("margin_px", 0), "isolate_after_batch_failure")
                res2 = parsed2[r["region_id"]]
                out[r["region_id"]] = res2 | {"trace": {"calls": out[r["region_id"]]["trace"]["calls"] + [rec2["call_id"]],
                                                        "seconds": rec["seconds"] + rec2["seconds"],
                                                        "finish_reason": rec2["finish_reason"],
                                                        "batch_failure": out[r["region_id"]]["contract_errors"]}}
        return out

    def read_pages(self, records: list[dict], max_images: int, margin_rule: str) -> dict[str, dict]:
        """READY 페이지의 모든 Region 을 판독한다. 반환: region_id → 결과."""
        work = []
        images = {}
        for record in records:
            if record["status"] not in READABLE_STATUSES or not record["regions"]:
                continue
            pid = record["page"]["page_id"]
            images[pid] = Image.open(record["page"]["image_path"]).convert("RGB")
            margin = vlm_tasks.page_margin(record) if margin_rule != "none" else 0
            for job in plan_page(record, max_images, margin):
                work.append((record, job))
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(self.run_job, rec, images[rec["page"]["page_id"]], job) for rec, job in work]
            for f in futures:
                results.update(f.result())
        return results


    # ---- 페이지 우선 판독(page/2) ----
    def _send(self, record: dict, req: dict, kind: str, max_tokens: int, extra: dict) -> tuple[dict, dict]:
        meta = {"kind": kind, "page_id": record["page"]["page_id"], "region_ids": req["region_ids"],
                "version": req.get("version"), "items": req.get("items"), "image_roles": req.get("image_roles")} | extra
        call = self.client.chat(req["text"], req["images"], req["schema"], f"{kind}_read", max_tokens, meta)
        rec = asdict(call)
        dump(self.trace_dir / "vlm" / f"{call.call_id}.json", rec)
        if kind == "page":
            parsed = page_read.validate_page(call, req)
        elif kind == "patch":
            parsed = page_read.validate_patch(call, req)
        elif kind == "reread":
            parsed = page_read.validate_reread(call, req)
        elif kind == "table_cells":
            parsed = table_cells.validate(call, req)
        else:
            parsed = vlm_tasks.validate(call, req)
        return rec, parsed

    def page_job(self, record: dict, img: Image.Image, view: dict, with_ocr: bool, regions: list[dict]) -> list:
        if view["view_id"] in self.resume:
            return [self.resume[view["view_id"]]]
        req = page_read.page_request(record, view, img, with_ocr, regions)
        rec, parsed = self._send(record, req, "page", page_read.PAGE_MAX_TOKENS,
                                 {"view_id": view["view_id"], "view_crop": view["crop"], "table_ids": req["table_ids"],
                                  "band_of": view.get("band_of"), "band_plan": view.get("band_plan"), "attempt": "page"})
        return [(rec, parsed)]

    def verify_job(self, record: dict, img: Image.Image, regions: list[dict], kind: str, page_texts: dict,
                   reasons: dict, view: dict | None = None) -> list:
        if kind == "reread":
            # docs/10 채택 조건 B: 원본 영역 crop + 1차 판독 단위(페이지·띠·타일) 문맥 이미지, 한 요청에 한 영역.
            r = regions[0]
            req = page_read.reread_request(record, r, img, page_texts.get(r["region_id"]), view)
            return [self._send(record, req, "reread", page_read.REREAD_MAX_TOKENS,
                               {"attempt": "region_reread_after_page", "verify_reasons": reasons, "condition": req["condition"],
                                "context_view": view})]
        if kind == "table_cells":
            req = table_cells.request(record, regions[0], img, view)
            return [self._send(record, req, "table_cells", table_cells.MAX_TOKENS,
                               {"attempt": "table_cells_after_page", "verify_reasons": reasons, "context_view": view})]
        if kind == "table":
            req = vlm_tasks.table_request(record, regions[0], img, vlm_tasks.TABLE_REQUEST_VERSION)
            extra = {"attempt": "table_check_after_page", "verify_reasons": reasons}
            return [self._send(record, req, "table", TABLE_MAX_TOKENS, extra)]
        if kind == "patch":
            req = page_read.patch_request(record, regions, img, page_texts)
            rec, parsed = self._send(record, req, "patch", page_read.VERIFY_MAX_TOKENS,
                                     {"attempt": "region_patch_after_page", "verify_reasons": reasons})
            out = [(rec, parsed)]
            if len(regions) > 1 and all(not p["contract_ok"] for p in parsed.values()):
                for r in regions:            # 묶음 실패 시 단독 1회(공통 실패 처리)
                    req1 = page_read.patch_request(record, [r], img, {r["region_id"]: page_texts.get(r["region_id"])})
                    out.append(self._send(record, req1, "patch", page_read.VERIFY_MAX_TOKENS,
                                          {"attempt": "isolate_after_batch_failure",
                                           "verify_reasons": {r["region_id"]: reasons.get(r["region_id"])}}))
            return out
        req = page_read.verify_request(record, regions, img, page_texts)
        rec, parsed = self._send(record, req, "verify", page_read.VERIFY_MAX_TOKENS,
                                 {"attempt": "region_verify_after_page", "verify_reasons": reasons})
        out = [(rec, parsed)]
        if len(regions) > 1 and all(not p["contract_ok"] for p in parsed.values()):
            # 일반 영역 crop 경로와 같은 실패 처리: 묶음이 통째로 실패하면 영역마다 1회 단독 요청한다.
            for r in regions:
                req1 = page_read.verify_request(record, [r], img, {r["region_id"]: page_texts.get(r["region_id"])})
                out.append(self._send(record, req1, "verify", page_read.VERIFY_MAX_TOKENS,
                                      {"attempt": "isolate_after_batch_failure",
                                       "verify_reasons": {r["region_id"]: reasons.get(r["region_id"])}}))
        return out

    def read_pages_page_first(self, records: list[dict], max_images: int, with_ocr: bool = True) -> dict[str, dict]:
        """1) 페이지(긴 이미지는 타일) 전체 판독 → 2) 일반 조건에 걸린 영역만 영역 crop 확인 → 3) 병합."""
        images, page_jobs = {}, []
        for record in records:
            if record["status"] not in READABLE_STATUSES or not record["regions"]:
                continue
            pid = record["page"]["page_id"]
            images[pid] = Image.open(record["page"]["image_path"]).convert("RGB")
            for view in record["route"]["views"]:
                regs = page_read.view_regions(record, view)
                if regs:
                    # (나) 크게 줄어드는 입력은 VLM 입력만 띠로 나눈다(OCR·영역 그대로).
                    for bview, bregs in page_read.band_views(view, regs):
                        if bregs:
                            page_jobs.append((record, bview, bregs))
        entries: list = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(self.page_job, rec, images[rec["page"]["page_id"]], view, with_ocr, regs)
                       for rec, view, regs in page_jobs]
            done = [f.result() for f in futures]
        verify_work = []
        for (record, view, regs), calls in zip(page_jobs, done):
            entries += calls
            parsed = calls[-1][1]
            patch, full, tables, reasons, texts = [], [], [], {}, {}
            for r in regs:
                res = parsed[r["region_id"]]
                why = page_read.verify_reasons(record, r, view, res)
                if self.table_read == "cells" and r["table_candidate"]:
                    # table-cells/1: 표 후보는 모두 표 하나씩 text·셀 구조를 함께 다시 읽는다(일반 재판독으로 보내지 않음).
                    verify_work.append((record, [r], "table_cells", {}, {r["region_id"]: why or ["table_structure_read"]}, view))
                    continue
                if why:
                    reasons[r["region_id"]] = why
                    texts[r["region_id"]] = res.get("text")
                    table_bad = r["table_candidate"] and (
                        not res.get("contract_ok") or res.get("is_table") is None
                        or bool({"table_rows_missing", "table_rows_incomplete"} & set(why)))
                    if table_bad:
                        tables.append(r)       # 페이지 판독의 표 결과가 없거나 불완전할 때만 표 crop 재판독
                    elif res.get("contract_ok") and res.get("text") is not None:
                        patch.append(r)        # (가) 1차 판독이 있으면 보완만
                    else:
                        full.append(r)         # 1차 판독이 없으면 crop 전체 판독
            if self.verify_mode == "reread":
                for r in patch + full:     # 1차 판독이 없어도(출력 실패·ID 누락) 같은 재판독으로 완결 후보를 받는다
                    verify_work.append((record, [r], "reread", {r["region_id"]: texts[r["region_id"]]},
                                        {r["region_id"]: reasons[r["region_id"]]}, view))
            else:
                for group, kind in ((patch, "patch"), (full, "verify")):
                    for i in range(0, len(group), max_images):
                        chunk = group[i:i + max_images]
                        verify_work.append((record, chunk, kind, {k["region_id"]: texts[k["region_id"]] for k in chunk},
                                            {k["region_id"]: reasons[k["region_id"]] for k in chunk}, view))
            for r in tables:
                verify_work.append((record, [r], "table", {}, {r["region_id"]: reasons[r["region_id"]]}, view))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(self.verify_job, rec, images[rec["page"]["page_id"]], regs, kind, texts, why, view)
                       for rec, regs, kind, texts, why, view in verify_work]
            for f in futures:
                entries += f.result()
        entries.sort(key=lambda e: e[0]["call_id"])
        return merge_entries(entries, region_index(records))


def region_index(records: list[dict] | None) -> dict[str, tuple]:
    return {r["region_id"]: (rec, r) for rec in records or [] for r in rec["regions"]}


def merge_entries(entries: list, index: dict[str, tuple] | None = None) -> dict[str, dict]:
    """호출 순서대로 영역 결과를 합친다. 계약을 통과한 가장 나중 결과를 쓰고, 통과한 결과가 없으면 가장 나중 결과를 쓴다.

    페이지 판독 뒤 영역 확인으로 문구가 바뀌면 1차 페이지 판독 문구(page_text)와 확인 사유를 trace 에 남긴다.
    재판독(reread)은 page_read.select_reread 의 고정 정책으로 1차와 재판독 후보 중 하나를 고른다(둘 다 trace 에 보존)."""
    out: dict[str, dict] = {}
    for rec, parsed in entries:
        meta = rec["request_meta"]
        for rid, res in parsed.items():
            prev = out.get(rid)
            tr = dict((prev or {}).get("trace") or {})
            tr["calls"] = tr.get("calls", []) + [rec["call_id"]]
            tr["kinds"] = tr.get("kinds", []) + [meta.get("kind")]
            tr["finish_reason"] = rec["finish_reason"]
            if (meta.get("verify_reasons") or {}).get(rid):
                tr["verify_reasons"] = meta["verify_reasons"][rid]
            if meta.get("kind") == "page":
                tr["page_text"] = res.get("text")
                tr["page_view"] = meta.get("view_id")
                if res.get("rows") is not None or res.get("is_table") is not None:
                    tr["page_rows"], tr["page_is_table"] = res.get("rows"), res.get("is_table")
            if meta.get("kind") == "table_cells":
                # text·셀 구조는 이 응답 하나의 묶음으로만 쓴다. 실패하면 이전의 온전한 묶음(1차 page 응답의 text·rows)을
                # 그대로 두고 표시한다. 부분 응답을 조합하지 않는다.
                tr |= {"table_read": meta.get("version") or table_cells.VERSION, "table_cells_errors": res.get("contract_errors") or None,
                       "table_notes": res.get("notes"), "context_view": (meta.get("context_view") or {}).get("view_id")}
                if res.get("contract_ok"):
                    out[rid] = {k: v for k, v in res.items() if k != "notes"} | {"trace": tr}
                else:
                    base = prev if prev is not None and prev.get("contract_ok") else res
                    out[rid] = base | {"table_structure_failed": True, "trace": tr}
                continue
            if meta.get("kind") == "reread":
                record, region = (index or {}).get(rid, (None, None))
                if record is None:
                    raise RuntimeError(f"{rid}: 재판독 후보 선택에 Stage 1 기록이 필요하다")
                first = (prev or {}).get("text")
                sel = page_read.select_reread(record, region, first, res)
                tr |= {"first_text": first, "reread_text": res.get("text"), "reread_source": sel["source"],
                       "reread_changes": res.get("changes"), "reread_notes": res.get("notes"),
                       "reread_measures": sel.get("measures"), "reread_condition": meta.get("condition")}
                base = dict(prev or {"region_id": rid, "contract_errors": [], "contract_ok": False})
                if sel["source"] == "reread":
                    base |= {"text": sel["text"], "uncertain": res.get("uncertain"), "contract_ok": True, "contract_errors": []}
                out[rid] = base | {"reread_review": sel["review"], "trace": tr}
                continue
            if prev is not None and prev.get("contract_ok") and not res.get("contract_ok"):
                out[rid] = prev | {"trace": tr | {"later_failed_call": rec["call_id"]}}
                continue
            if meta.get("kind") == "patch":
                if prev is None or prev.get("text") is None:
                    out[rid] = (prev or {}) | {"trace": tr | {"patch_without_page_text": rec["call_id"]}}
                    continue
                new_text, applied, failed = page_read.apply_patch(prev["text"], res.get("patch") or {})
                out[rid] = prev | {"text": new_text,
                                   "uncertain": bool(prev.get("uncertain")) or bool(res.get("uncertain")),
                                   "patch_not_applied": failed} | {"trace": tr | {"patch_applied": applied,
                                                                                    "patch_not_applied": failed}}
                continue
            keep = {k: prev[k] for k in ("is_table", "rows") if prev and k in prev and k not in res}
            out[rid] = prev_fields(prev, res, meta) | keep | {"trace": tr}
    return out


def prev_fields(prev: dict | None, res: dict, meta: dict) -> dict:
    """영역 확인(verify)은 문구만 다시 받는다. 표 후보 필드(is_table·rows)는 페이지 판독 값을 유지한다."""
    if prev is not None and meta.get("kind") == "verify":
        return {k: v for k, v in prev.items() if k in ("is_table", "rows")} | res
    return res


def read_path(trace: dict | None) -> str:
    kinds = (trace or {}).get("kinds") or []
    if "page" not in kinds:
        return "crop+table_cells" if "table_cells" in kinds else "crop"
    if len(kinds) == 1:
        return "page"
    return "page+" + "+".join(k for k in kinds[1:])


def reconstruct_reads(trace_dir: Path, records: list[dict] | None = None) -> dict[str, dict]:
    """저장된 VLM 호출 기록(trace/vlm)을 같은 계약 검사로 다시 통과시켜 판독 결과를 복원한다(호출 없음).
    같은 Region 을 여러 번 호출했으면(묶음 실패 후 단독) 마지막 호출 결과를 쓴다."""
    from types import SimpleNamespace
    from .jsonio import load
    entries = []
    for path in sorted((trace_dir / "vlm").glob("*.json")):
        rec = load(path)
        meta = rec["request_meta"]
        call = SimpleNamespace(error=rec["error"], parsed=rec["parsed"])
        if meta["kind"] == "page":
            parsed = page_read.validate_page(call, {"region_ids": meta["region_ids"], "table_ids": meta.get("table_ids") or []})
        elif meta["kind"] == "patch":
            parsed = page_read.validate_patch(call, {"region_ids": meta["region_ids"]})
        elif meta["kind"] == "reread":
            parsed = page_read.validate_reread(call, {"region_ids": meta["region_ids"]})
        elif meta["kind"] == "table_cells":
            parsed = table_cells.validate(call, {"region_ids": meta["region_ids"]})
        else:
            parsed = vlm_tasks.validate(call, {"mode": "table" if meta["kind"] == "table" else "crop",
                                               "region_ids": meta["region_ids"]})
        entries.append((rec, parsed))
    return merge_entries(entries, region_index(records))


def integrate_region(record: dict, region: dict, res: dict | None) -> dict:
    """Region 하나의 최종 결과. 실패도 bbox·사유와 함께 남긴다(예외로 페이지를 멈추지 않는다)."""
    if res is None:
        return {"region_id": region["region_id"], "page_id": region["page_id"], "bbox": region["bbox"],
                "source": region["source"], "source_index": region["source_index"],
                "table_candidate": region["table_candidate"], "text": None, "text_status": "not_read",
                "table": None if not region["table_candidate"] else {"status": "unknown", "rows": None},
                "needs_review": True, "review": {"quality": ["not_read"], "context": []}, "trace": None,
                "ocr_reference": failed_ocr_reference(record, region)}
    res = rows_text_fallback(region, res)
    review = vlm_tasks.review_reasons(record, region, res)
    if str(region.get("source", "")).startswith("supplement:"):
        # 레이아웃 상자가 놓친 글자로 만든 보충 영역(docs/16 §9). 원래 상자 영역이 아니므로 맥락 검토 근거로 남긴다.
        review["context_reasons"] += ["supplement_region", region["source"].replace("supplement:", "supplement_")]
        if region.get("supplement_note"):
            review["context_reasons"].append(region["supplement_note"])
    if res.get("text_from_rows"):
        review["context_reasons"].append("text_from_table_rows")
    if res.get("patch_not_applied"):
        review["quality_reasons"].append("patch_not_applied")
    review["quality_reasons"] += [x for x in (res.get("reread_review") or []) if x not in review["quality_reasons"]]
    kinds = (res.get("trace") or {}).get("kinds") or []
    if res.get("text") and {"page", "reread", "table_cells"} & set(kinds):
        # 문맥 이미지(페이지·띠·타일)를 본 판독 뒤에도 남은 상자 밖 OCR 문구·반복 문구는 품질 검토로 표시한다(문구는 고치지 않음).
        own = page_read.region_ocr(record, region)
        if page_read.outside_text(record, region, res["text"], {o["ocr_id"] for o in own}):
            review["quality_reasons"].append("text_from_outside_box")
        if page_read.repeated_text([o["ocr_text"] for o in own], res["text"]):
            review["quality_reasons"].append("repeated_text")
    if res.get("text"):
        # 단순 세로 배치 영역의 줄 순서가 위→아래와 어긋남(simple-column-order/1). 읽기 모드와 무관하게 최종 문구에 계산한다.
        oc = page_read.order_conflict(record, region, res["text"])
        if oc:
            review["quality_reasons"].append("reading_order_conflict")
            review["measures"]["reading_order"] = oc
    if res.get("table_structure_failed"):
        review["quality_reasons"].append("table_structure_failed")
    if region["table_candidate"] and "page" in kinds and not {"table", "table_cells"} & set(kinds) \
            and {"reread", "verify", "patch"} & set(kinds) and res.get("text") != (res.get("trace") or {}).get("page_text") \
            and res.get("is_table") is True and res.get("rows"):
        # legacy 경로: 영역 재판독이 표 text 만 바꾸고 rows 는 1차 page 응답 값이 남은 경우(서로 다른 판독의 결합)
        review["quality_reasons"].append("table_text_rows_from_different_reads")
    text = res.get("text")
    status = "read_failed" if text is None else ("no_text" if text == "" else "read")
    table = None
    if region["table_candidate"]:
        is_table = res.get("is_table")
        table = {"status": "table" if is_table is True else ("not_table" if is_table is False else "unknown"),
                 "rows": res.get("rows") if is_table is True else None}
        if res.get("cells") is not None:
            # table-cells/1: 셀 구조가 원본 결과, rows 는 cells 에서 변환한 호환 보기
            table |= {"structure": (res.get("trace") or {}).get("table_read") or table_cells.VERSION,
                      "n_rows": res.get("n_rows"), "n_cols": res.get("n_cols"),
                      "cells": res.get("cells") if is_table is True else None}
        elif table["status"] != "unknown":
            table["structure"] = "rows/legacy"
    out = {"region_id": region["region_id"], "page_id": region["page_id"], "bbox": region["bbox"],
            "source": region["source"], "source_index": region["source_index"],
            "table_candidate": region["table_candidate"], "text": text, "text_status": status, "table": table,
            "needs_review": bool(review["quality_reasons"] or review["context_reasons"]),
            "review": {"quality": review["quality_reasons"], "context": review["context_reasons"],
                       "measures": review["measures"]},
            "contract_ok": res.get("contract_ok"), "contract_errors": res.get("contract_errors"),
            "uncertain": res.get("uncertain"), "trace": res.get("trace"), "read_path": read_path(res.get("trace"))}
    if status == "read_failed":
        out["ocr_reference"] = failed_ocr_reference(record, region)
    return out


def rows_text_fallback(region: dict, res: dict) -> dict:
    """표 확인 응답이 rows 는 채우고 text 를 빈 문자열로 둔 경우, rows 를 행 단위로 이어 text 로 쓴다.

    v2·v3 판독에서 rows 는 정확한데 text 만 비운 사례(f02 L001, 2회 모두)가 있었다. 새 문구를 만들지 않고 같은 응답의
    rows(VLM 이 이미지를 보고 쓴 셀)만 옮긴다. 결과에는 맥락 근거 text_from_table_rows 로 표시한다."""
    rows = res.get("rows")
    if region["table_candidate"] and res.get("is_table") is True and res.get("text") == "" and rows \
            and any(c.strip() for row in rows for c in row):
        return res | {"text": "\n".join(" ".join(c for c in row if c.strip()) for row in rows), "text_from_rows": True}
    return res


def failed_ocr_reference(record: dict, region: dict) -> dict:
    """판독 실패 영역의 OCR 원문(배정 + 경계에 걸친 미배정). 최종 문구가 아니며 text 는 null 로 남는다."""
    ctx = vlm_tasks.region_context(record, region)
    return {"source": "prunedResult.overall_ocr_res", "final": False, "assigned": ctx["ocr_draft"],
            "boundary_unassigned": [b["text"] for b in ctx["boundary_ocr"]],
            "note": "VLM 판독 실패로 최종 문구 없음. 검토용 OCR 원문"}
