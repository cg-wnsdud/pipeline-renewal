"""놓친 영역 보충(supplement/1, docs/16 §9). VLM 판독 뒤, 레이아웃 상자가 놓친 글자로 보충 영역을 만들어 원본에서 읽고 기존 영역과
중복이 아니면 파싱 결과에 더한다. 기존 레이아웃 영역(…-L000)의 ID·bbox 는 바꾸지 않는다.

페이지마다:
1. 놓친 OCR: 결과 미배정 OCR(어느 영역에도 면적 50% 이상 들지 않은 줄) 중 정규화 2자 이상. 영역에 걸친 줄은 후보 영역 최종 문구에 이미
   있으면 뺀다(로고 'X' 접두·따옴표·OCR 오타 차이를 감안해 6자 이상은 편집거리 허용, 짧은 줄은 그대로 포함될 때).
2. 표 상자: 놓친 OCR 이 2개 이상인 view 만 같은 crop·옵션에 layoutThreshold=0.3 을 더해 OCR 을 한 번 더 받아 table 상자만 쓴다
   (서버 클래스별 문턱은 막혀 있어 이렇게 표 클래스만 낮춘다, docs/06). 놓친 OCR 2개 이상을 덮고, 기존 영역이 덮는 비율 20% 미만,
   페이지 면적 50% 미만인 상자만.
3. 나머지 놓친 OCR 을 같은 줄(세로 겹침 ≥ 작은 줄 높이 50%, 가로 간격 ≤ 6×줄 높이)·위아래(세로 간격 ≤ 줄 높이, 가로 겹침)로 묶는다.
   2행 이상이고 2개 행 이상이 2열 이상이면 표 후보.
4. 판독 전 중복: 기존 영역이 면적 50% 이상 덮는 후보는 버린다.
5. 판독: 표 후보는 table-cells/1(crop + 문맥), 그 밖은 crop/3(영역 crop 묶음). 후보 안 미배정 OCR 은 그 영역의 배정 OCR 로 넘긴다.
   표 판독이 구조 검사에 실패하면 한 번 더 표로 읽고, 또 실패하면 일반 crop 판독으로 문구만 받아 글자 영역으로 더한다
   (supplement_table_structure_failed). 본 판독은 앞선 페이지 판독으로 되돌아가지만 보충 영역에는 앞선 판독이 없다(tt03 왼쪽 표:
   문구는 맞고 격자 밖 빈 셀 하나로 구조 검사 실패).
6. 판독 뒤 중복: 판독 실패·빈 문구는 버리고, 문구가 이웃 영역 최종 문구에 이미 있으면 버린다. 이웃은 6자 이상 문구면 후보를 줄 높이만큼
   넓힌 범위와 겹치는 영역, 짧은 문구면 후보 상자와 실제로 겹치는 영역(짧은 낱말은 다른 위치의 같은 낱말과 구별할 수 없다: tt03 '기본금리'
   항목명과 옆 영역 'NH고향사랑기부적금 기본금리'). 버린 후보의 OCR 은 미배정으로 남는다.
영역 ID 는 <view_id>-S000…(후보 번호, 버린 후보 번호는 비어 있을 수 있음), source 는 supplement:ocr_gap | supplement:table_threshold.
판단 근거는 trace/supplement.json, 표 상자 응답은 trace/supplement/ocr-*.json, 판독 호출은 trace/vlm(재구성 가능).
"""
from __future__ import annotations

import base64
import copy
import io
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

from .final_output import in_region_owner, output_unassigned
from .geometry import area, intersection
from .jsonio import dump, load
from .ocr_client import BASE_OPTIONS
from .text_eval import norm, semiglobal, tolerance

VERSION = "supplement/1"
TABLE_THRESHOLD = 0.3
MIN_OCR_FOR_TABLE = 2
MAX_TABLE_REGION_OVERLAP = 0.2
MAX_TABLE_PAGE_FRACTION = 0.5
DUP_OVERLAP = 0.5
SHORT_TEXT = 6                     # 이보다 짧은 문구는 그대로 포함될 때만 '이미 있음'
SOURCE_GAP, SOURCE_TABLE = "supplement:ocr_gap", "supplement:table_threshold"
PLAN_FILE = "supplement.json"


def settings() -> dict:
    return {"version": VERSION, "table_threshold": TABLE_THRESHOLD, "min_ocr_for_table": MIN_OCR_FOR_TABLE,
            "max_table_region_overlap": MAX_TABLE_REGION_OVERLAP, "max_table_page_fraction": MAX_TABLE_PAGE_FRACTION,
            "duplicate_overlap": DUP_OVERLAP, "short_text": SHORT_TEXT,
            "group_rule": "same-row(v-overlap>=0.5h, gap<=6h) | stacked(v-gap<=1h, h-overlap)",
            "grid_rule": "rows>=2 and >=2 rows with >=2 OCR", "read": {"table": "table-cells/1", "text": "crop/3"}}


def contains(q: str, t: str) -> bool:
    """정규화 문구 q 가 t 에 이미 있는가(짧은 문구는 그대로, 긴 문구는 편집거리 허용)."""
    if not q or not t:
        return False
    return q in t if len(q) < SHORT_TEXT else semiglobal(q, t) <= tolerance(q)


def inside(box, region) -> float:
    return intersection(box, region) / max(area(box), 1)


def covered_fraction(box, boxes, n: int = 40) -> float:
    """box 면적 중 boxes(겹침은 한 번만)가 덮는 비율(격자 표본)."""
    x0, y0, x1, y1 = box
    hit = 0
    for i in range(n):
        x = x0 + (i + 0.5) * (x1 - x0) / n
        for j in range(n):
            y = y0 + (j + 0.5) * (y1 - y0) / n
            hit += any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)
    return hit / (n * n)


def linked(a, b) -> bool:
    h = min(a[3] - a[1], b[3] - b[1])
    same_row = min(a[3], b[3]) - max(a[1], b[1]) >= 0.5 * h and max(a[0], b[0]) - min(a[2], b[2]) <= 6 * h
    stacked = max(a[1], b[1]) - min(a[3], b[3]) <= h and min(a[2], b[2]) - max(a[0], b[0]) > 0
    return same_row or stacked


def clusters(items: list[dict]) -> list[list[dict]]:
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if linked(items[i]["ocr_bbox"], items[j]["ocr_bbox"]):
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, u in enumerate(items):
        groups.setdefault(find(i), []).append(u)
    return [sorted(g, key=lambda u: (u["ocr_bbox"][1], u["ocr_bbox"][0])) for g in groups.values()]


def is_grid(items: list[dict]) -> bool:
    rows: list[list[dict]] = []
    for u in sorted(items, key=lambda u: u["ocr_bbox"][1]):
        b = u["ocr_bbox"]
        for row in rows:
            r = row[0]["ocr_bbox"]
            if min(b[3], r[3]) - max(b[1], r[1]) >= 0.5 * min(b[3] - b[1], r[3] - r[1]):
                row.append(u)
                break
        else:
            rows.append([u])
    return len(rows) >= 2 and sum(len(r) >= 2 for r in rows) >= 2


def _clip(b, w, h) -> list[float]:
    return [round(max(0.0, b[0]), 1), round(max(0.0, b[1]), 1), round(min(float(w), b[2]), 1), round(min(float(h), b[3]), 1)]


def _padded(items: list[dict], w: int, h: int) -> list[float]:
    hs = [u["ocr_bbox"][3] - u["ocr_bbox"][1] for u in items]
    pad = min(8.0, max(2.0, round(0.25 * statistics.median(hs))))
    return _clip([min(u["ocr_bbox"][0] for u in items) - pad, min(u["ocr_bbox"][1] for u in items) - pad,
                  max(u["ocr_bbox"][2] for u in items) + pad, max(u["ocr_bbox"][3] for u in items) + pad], w, h)


def missed_items(rec: dict, texts: dict[str, str]) -> list[dict]:
    out = []
    for u in output_unassigned(rec):
        q = norm(u["ocr_text"])
        if len(q) < 2:
            continue
        if u["reason"] != "outside_layout" and any(contains(q, texts.get(c["region_id"], "")) for c in u.get("candidates") or []):
            continue
        out.append(u)
    return out


class TableProbe:
    """같은 view crop·옵션에 layoutThreshold 만 더한 OCR 요청 1회 → table 상자(페이지 좌표). 서버 설정은 바꾸지 않는다."""

    def __init__(self, layout_options: dict | None, trace_dir: Path):
        self.url, self.timeout = os.environ["PN_OCR_URL"], float(os.environ.get("PN_OCR_TIMEOUT_S", "300"))
        self.options = dict(BASE_OPTIONS) | (layout_options or {}) | {"layoutThreshold": TABLE_THRESHOLD}
        self.trace_dir, self.calls = trace_dir, 0

    def __call__(self, page_img: Image.Image, view: dict) -> dict:
        x0, y0, x1, y1 = view["crop"]
        buf = io.BytesIO()
        page_img.crop((x0, y0, x1, y1)).save(buf, format="PNG")
        t0 = time.perf_counter()
        self.calls += 1
        try:
            resp = requests.post(self.url, json={"fileType": 1, "file": base64.b64encode(buf.getvalue()).decode("ascii"), **self.options},
                                 timeout=(10, self.timeout))
            resp.raise_for_status()
            det = resp.json()["result"]["layoutParsingResults"][0]["prunedResult"]["layout_det_res"]
            boxes = [{"score": round(float(b["score"]), 4),
                      "bbox": [float(b["coordinate"][0]) + x0, float(b["coordinate"][1]) + y0,
                               float(b["coordinate"][2]) + x0, float(b["coordinate"][3]) + y0]}
                     for b in det["boxes"] if b.get("label") == "table"]
            out = {"view_id": view["view_id"], "table_boxes": boxes, "error": None}
        except Exception as e:  # noqa: BLE001 — 보충 실패로 파싱 전체를 멈추지 않는다(근거에 남김)
            out = {"view_id": view["view_id"], "table_boxes": [], "error": f"{type(e).__name__}: {e}"[:300]}
        out["seconds"] = round(time.perf_counter() - t0, 3)
        dump(self.trace_dir / "supplement" / f"ocr-{view['view_id']}.json", out | {"options": self.options})
        return out


def plan_page(rec: dict, texts: dict[str, str], probe, page_img) -> dict:
    """한 페이지의 보충 후보(판독 전). probe=None 이면 표 상자 조회 없이 OCR 묶음만."""
    w, h = rec["page"]["width"], rec["page"]["height"]
    items = missed_items(rec, texts)
    existing = [r["bbox"] for r in rec["regions"]]
    cands, probes, used = [], [], set()
    if probe is not None and page_img is not None:
        for view in rec["route"]["views"]:
            in_view = [u for u in items if u["view_id"] == view["view_id"]]
            if len(in_view) < MIN_OCR_FOR_TABLE:
                continue
            res = probe(page_img, view)
            entry = {"view_id": view["view_id"], "error": res["error"], "seconds": res["seconds"], "boxes": []}
            for tb in sorted(res["table_boxes"], key=lambda x: -x["score"]):
                bb = _clip(tb["bbox"], w, h)
                covered = [u for u in in_view if u["ocr_id"] not in used and inside(u["ocr_bbox"], bb) >= 0.5]
                ov, frac = covered_fraction(bb, existing), area(bb) / (w * h)
                why = ("covers_too_few_missed_ocr" if len(covered) < MIN_OCR_FOR_TABLE else
                       "overlaps_existing_regions" if ov >= MAX_TABLE_REGION_OVERLAP else
                       "page_size_container" if frac >= MAX_TABLE_PAGE_FRACTION else None)
                entry["boxes"].append({"score": tb["score"], "bbox": bb, "covered_missed_ocr": len(covered), "region_overlap": round(ov, 3),
                                       "page_fraction": round(frac, 3), "accepted": why is None, "why": why})
                if why is None:
                    used |= {u["ocr_id"] for u in covered}
                    cands.append({"origin": "table_threshold", "bbox": bb, "view_id": view["view_id"], "table": True, "score": tb["score"]})
            probes.append(entry)
    by_view: dict[str, list[dict]] = {}
    for u in items:
        if u["ocr_id"] not in used:
            by_view.setdefault(u["view_id"], []).append(u)
    for view_id, us in by_view.items():
        for g in clusters(us):
            cands.append({"origin": "ocr_gap", "bbox": _padded(g, w, h), "view_id": view_id, "table": is_grid(g), "score": None})
    for k, c in enumerate(cands):
        c["region_id"] = f"{c['view_id']}-S{k:03d}"
        c["existing_overlap"] = round(covered_fraction(c["bbox"], existing), 3)
        c["decision"], c["why"] = ("drop", "overlaps_existing_region") if c["existing_overlap"] >= DUP_OVERLAP else ("read", None)
    return {"page_id": rec["page"]["page_id"], "missed_ocr": [u["ocr_id"] for u in items], "table_probes": probes, "candidates": cands}


def attach(rec: dict, cands: list[dict]) -> None:
    """후보를 영역으로 더하고, 그 안의 결과 미배정 OCR(면적 50% 이상)을 배정 OCR 로 옮긴다(메모리의 기록만, 파일은 그대로)."""
    views = {v["view_id"]: v for v in rec["route"]["views"]}
    moved: set[str] = set()
    free = [u for u in rec["unassigned_texts"] if in_region_owner(u) is None]
    for c in cands:
        bb = c["bbox"]
        dx, dy = views[c["view_id"]]["crop"][:2]
        ocr = [u for u in free if u["ocr_id"] not in moved and inside(u["ocr_bbox"], bb) >= 0.5]
        moved |= {u["ocr_id"] for u in ocr}
        rec["regions"].append({
            "region_id": c["region_id"], "page_id": rec["page"]["page_id"], "view_id": c["view_id"],
            "source_index": int(c["region_id"].rsplit("-S", 1)[1]), "bbox": bb, "raw_coordinate": [bb[0] - dx, bb[1] - dy, bb[2] - dx, bb[3] - dy],
            "table_candidate": c["table"], "raw_label": "supplement_table" if c["table"] else "supplement_text", "raw_cls_id": None,
            "raw_score": c["score"] or 0.0, "source": SOURCE_TABLE if c["origin"] == "table_threshold" else SOURCE_GAP,
            "supplement_note": c.get("fallback"),
            "assigned_ocr_ids": [u["ocr_id"] for u in ocr], "crossing_ocr_ids": [], "ambiguous_ocr_ids": [],
            "ocr_draft": "\n".join(u["ocr_text"] for u in ocr), "text": None, "text_status": "not_read"})
        for u in ocr:
            ins = inside(u["ocr_bbox"], bb)
            rec["assignments"].append({"ocr_id": u["ocr_id"], "region_id": c["region_id"], "page_id": u["page_id"], "view_id": u["view_id"],
                                       "ocr_text": u["ocr_text"], "ocr_bbox": u["ocr_bbox"], "score": u["score"], "empty_text": u["empty_text"],
                                       "exact_inside": ins >= 0.999, "protrusion_px": 0.0, "inside_ratio": round(ins, 4),
                                       "assignment_review": [], "supplement": True})
        for r in rec["regions"][:-1]:
            i = intersection(r["bbox"], bb)
            if i > 0:
                a1, a2 = area(r["bbox"]), area(bb)
                rec["diagnostics"]["region_overlaps"].append({
                    "a": r["region_id"], "b": c["region_id"], "relation": "overlap", "intersection_px": round(i),
                    "iou": round(i / (a1 + a2 - i), 4), "frac_of_a": round(i / a1, 4), "frac_of_b": round(i / a2, 4),
                    "box": [max(r["bbox"][0], bb[0]), max(r["bbox"][1], bb[1]), min(r["bbox"][2], bb[2]), min(r["bbox"][3], bb[3])],
                    "supplement": True})
    rec["unassigned_texts"] = [u for u in rec["unassigned_texts"] if u["ocr_id"] not in moved]
    pc = rec.get("partition_checks")
    if pc:
        pc["assigned"] = pc.get("assigned", 0) + len(moved)
        pc["unassigned"] = pc.get("unassigned", 0) - len(moved)


def _read_jobs(rec: dict, regions: list[dict], max_images: int) -> list[dict]:
    tables = [{"regions": [r], "kind": "table"} for r in regions if r["table_candidate"]]
    texts = [r for r in regions if not r["table_candidate"]]
    return tables + [{"regions": texts[k:k + max_images], "kind": "general"} for k in range(0, len(texts), max_images)]


def _decide(rec: dict, region: dict, res: dict | None, texts: dict[str, str]) -> tuple[bool, str | None]:
    text = (res or {}).get("text")
    if not res or not res.get("contract_ok") or text is None:
        return False, "read_failed"
    q = norm(text)
    if not q:
        return False, "no_text"
    hs = [a["ocr_bbox"][3] - a["ocr_bbox"][1] for a in rec["assignments"] if a["region_id"] == region["region_id"]]
    g = statistics.median(hs) if hs else 20.0
    b = region["bbox"]
    zone = [b[0] - g, b[1] - g, b[2] + g, b[3] + g]
    for r in rec["regions"]:
        if r["region_id"] == region["region_id"]:
            continue
        near = intersection(r["bbox"], zone) > 0 if len(q) >= SHORT_TEXT else intersection(r["bbox"], b) > 0
        if near and contains(q, texts.get(r["region_id"], "")):
            return False, f"text_in_region:{r['region_id']}"
    return True, None


def run(records: list[dict], reads: dict[str, dict], reader, probe, trace_dir: Path, max_images: int = 8) -> dict:
    """보충 단계 실행. records 와 reads 를 제자리에서 갱신(채택 후보만)하고 근거를 trace/supplement.json 에 쓴다."""
    texts = {rid: norm(r.get("text") or "") for rid, r in reads.items()}
    pages, work = [], []
    for rec in records:
        if rec["status"] not in ("READY", "CANDIDATE_RUN") or not rec["regions"]:
            continue
        with Image.open(rec["page"]["image_path"]) as im:
            img = im.convert("RGB")
        plan = plan_page(rec, texts, probe, img)
        pages.append((rec, plan, img))
        todo = [c for c in plan["candidates"] if c["decision"] == "read"]
        if todo:
            tmp = copy.deepcopy(rec)
            attach(tmp, todo)
            regions = [r for r in tmp["regions"] if r["region_id"] in {c["region_id"] for c in todo}]
            work += [(tmp, img, job) for job in _read_jobs(tmp, regions, max_images)]
    got: dict[str, dict] = {}

    def run_jobs(jobs):
        with ThreadPoolExecutor(max_workers=max(1, reader.concurrency)) as pool:
            for out in pool.map(lambda w: reader.run_job(*w), jobs):
                got.update(out)
    run_jobs(work)
    failed = lambda: [(tmp, img, r) for tmp, img, job in work if job["kind"] == "table" for r in job["regions"]  # noqa: E731
                      if not (got.get(r["region_id"]) or {}).get("contract_ok")]
    run_jobs([(tmp, img, {"regions": [r], "kind": "table"}) for tmp, img, r in failed()])           # 표 판독 재시도 1회
    fallback = failed()
    run_jobs([(tmp, img, {"regions": [r | {"table_candidate": False}], "kind": "general"}) for tmp, img, r in fallback])
    fallback_ids = {r["region_id"] for _, _, r in fallback}
    added = dropped = 0
    for rec, plan, _ in pages:
        accept = []
        for c in plan["candidates"]:
            if c["decision"] != "read":
                dropped += 1
                continue
            tmp_region = {"region_id": c["region_id"], "bbox": c["bbox"]}
            probe_rec = copy.deepcopy(rec)
            attach(probe_rec, accept + [c])            # 앞서 채택한 보충 영역까지 이웃으로 본다
            ok, why = _decide(probe_rec, tmp_region, got.get(c["region_id"]), texts)
            if c["region_id"] in fallback_ids:            # 표 구조 판독 실패 → 일반 판독 문구로 글자 영역
                c["table"], c["fallback"] = False, "supplement_table_structure_failed"
            c["decision"], c["why"] = ("add", None) if ok else ("drop", why)
            c["text"] = (got.get(c["region_id"]) or {}).get("text")
            if ok:
                accept.append(c)
                texts[c["region_id"]] = norm(c["text"])
                added += 1
            else:
                dropped += 1
        attach(rec, accept)
        for c in accept:
            reads[c["region_id"]] = got[c["region_id"]]
    summary = {"version": VERSION, "settings": settings(), "ocr_calls": getattr(probe, "calls", 0), "added": added, "dropped": dropped,
               "pages": [plan for _, plan, _ in pages]}
    dump(trace_dir / PLAN_FILE, summary)
    return summary


def reapply(records: list[dict], trace_dir: Path) -> int:
    """저장된 보충 근거(trace/supplement.json)의 채택 영역을 기록에 다시 더한다(재구성·화면용, 호출 없음). 더한 영역 수."""
    p = Path(trace_dir) / PLAN_FILE
    if not p.is_file():
        return 0
    by_page = {pg["page_id"]: [c for c in pg["candidates"] if c["decision"] == "add"] for pg in load(p)["pages"]}
    n = 0
    for rec in records:
        cands = by_page.get(rec["page"]["page_id"]) or []
        if cands and not any(r["region_id"] == cands[0]["region_id"] for r in rec["regions"]):
            attach(rec, cands)
            n += len(cands)
    return n
