"""입력 전체 검토 화면(정적 HTML) → <out>/report.html.

파일(입력)마다 한 화면: 왼쪽에 페이지 이미지와 영역 상자, 오른쪽에 영역별 최종 문구·상품 배정·라벨·검토 사유.
상자와 목록 항목은 서로 눌러 찾는다. 첫 화면은 전체 요약(파일별 Region·검토 수와 이번 실행의 소요시간).

읽는 것: files/index.json, files/<file_id>/result.json(layout-result/3, 상품·라벨이 없는 /2 도 그린다),
trace/semantic/<묶음>.json(라벨 근거 인용), report.json(시간), gold.json(있으면 묶음별 정답 템플릿 채점, make_pipeline_report --gold).
모델을 호출하지 않는다.
시간은 의미 단계 묶음(submission) 단위 기록이다. 묶음은 동시에 실행돼 서로 겹치고, 파싱 단계(OCR·판독) 시간은 들어 있지 않다.
`report.rebuild.json`(저장 응답 재생)은 실제 시간이 아니라서 `report.json` 이 없을 때만 경고와 함께 쓴다.
"""
from __future__ import annotations

import html
import os
from pathlib import Path
from urllib.parse import quote

from .jsonio import load

PALETTE = ["#1E88E5", "#8E24AA", "#F4511E", "#00897B", "#C0CA33", "#6D4C41", "#D81B60", "#3949AB"]
SHARED, MIXED, UNKNOWN, NOT_RUN = "#546E7A", "#6A1B9A", "#C62828", "#78909C"
PSTATUS = {"classified": "분류", "undetermined": "미확정", "unsupported": "미지원"}
PENSION = {"included": "포함", "not_exposed": "미노출", "undetermined": "미확정"}
VERDICT = {"correct": ("정답", "#2E7D32", "✓"), "partial": ("부분", "#EF6C00", "△"), "wrong": ("틀림", "#C62828", "✗"),
           "unscored": ("채점 제외", "#78909C", "·")}
PHASES = (("observe", "관측(상품·템플릿·영역 소속 결정)"), ("integrate", "통합(페이지별 상품 연결)"), ("label", "라벨링"))

esc = html.escape


def pipeline_entry(tm: dict, note: str = "") -> dict:
    """run_pipeline 의 trace/timing.json → 입력 검토 화면의 파이프라인 시간(OCR·판독·결과 저장·의미 단계·전체)."""
    s1, rd, sp = tm.get("stage1") or {}, tm.get("read") or {}, tm.get("supplement") or {}
    out = {"started_at": tm.get("started_at"), "ocr_wall_seconds": s1.get("wall_seconds", s1.get("ocr_wall_seconds")),
           "read_wall_seconds": rd.get("wall_seconds"), "read_http_calls": rd.get("http_calls"),
           "supplement_wall_seconds": sp.get("wall_seconds"),
           "supplement_note": (f"표 확인 OCR {sp.get('ocr_calls')}회 · 판독 {sp.get('vlm_calls')}회 · 추가 {sp.get('added')} · 버림 {sp.get('dropped')}"
                               if sp else ""),
           "parse_wall_seconds": tm.get("total_wall_seconds"), "semantic_wall_seconds": tm.get("semantic_stage_wall_seconds"),
           "end_to_end_wall_seconds": tm.get("end_to_end_wall_seconds"),
           "note": " · ".join(x for x in (note, tm.get("note")) if x)}     # tm.note: 재구성 실행 등 시간의 출처 설명
    if None not in (out["end_to_end_wall_seconds"], out["parse_wall_seconds"], out["semantic_wall_seconds"]):
        out["export_wall_seconds"] = round(out["end_to_end_wall_seconds"] - out["parse_wall_seconds"] - out["semantic_wall_seconds"], 3)
    return out


def _pipeline_timing(out: Path, subs: list[str]) -> tuple[dict, dict | None]:
    """(묶음별 파이프라인 시간, 실행 전체 시간). 합본 폴더는 pipeline_timing.json(묶음별), run_pipeline 의 semantic 폴더는
    상위 trace/timing.json(실행 전체: 묶음이 하나면 그 묶음 시간, 여럿이면 입력별로 나눌 수 없어 실행 전체로만 표시)."""
    if (out / "pipeline_timing.json").is_file():
        return load(out / "pipeline_timing.json")["submissions"], None
    tm = out.parent / "trace" / "timing.json"
    if out.name == "semantic" and tm.is_file():
        e = pipeline_entry(load(tm), "이 실행(run_pipeline) 전체")
        return ({subs[0]: e} if len(subs) == 1 else {}), (e if len(subs) > 1 else None)
    return {}, None


def build_report(out: Path) -> Path:
    out = Path(out)
    index = load(out / "files" / "index.json")
    rep, rep_path = {}, None
    for name in ("report.json", "report.rebuild.json"):
        if (out / name).is_file():
            rep, rep_path = load(out / name), name
            break
    run = load(out / "run.json") if (out / "run.json").is_file() else {}
    gold = load(out / "gold.json")["submissions"] if (out / "gold.json").is_file() else {}
    sub_timing = {s["submission_id"]: s.get("timing") for s in rep.get("submissions", [])}
    pipe, run_pipe = _pipeline_timing(out, list(dict.fromkeys(f["submission_id"] for f in index["files"])))
    for s, e in pipe.items():
        sub_timing[s] = (sub_timing.get(s) or {}) | {"pipeline": e}
    if run_pipe:
        run["pipeline"] = run_pipe
    files = sorted(index["files"], key=lambda f: (f["submission_id"], f["input_order"]))
    by_sub: dict[str, list[str]] = {}
    for f in files:
        by_sub.setdefault(f["submission_id"], []).append(f["file_id"])
    panes, rows = [], []
    for f in files:
        d = load(out / f["result"])
        pane, row = _pane(out, out / Path(f["result"]).parent, d, by_sub[f["submission_id"]], sub_timing.get(f["submission_id"]), rep,
                          gold.get(f["submission_id"]))
        panes.append(pane)
        rows.append(row)
    summary = _summary(out, rows, sub_timing, rep, rep_path, run, index, gold)
    mark = lambda r: (f"<b style='color:{VERDICT[r['verdict']][1]}'>{VERDICT[r['verdict']][2]}</b> " if r.get("verdict") else "")  # noqa: E731
    tabs = ["<button class='tab' data-key='summary' data-label='전체 요약'><b>전체 요약</b></button>"] + [
        f"<button class='tab' data-key='{esc(r['fid'])}' data-label='{esc(r['label'])}'>{mark(r)}{esc(r['label'])} "
        f"<small>{r['regions']} Region</small></button>" for r in rows]
    path = out / "report.html"
    path.write_text(_PAGE.replace("__TITLE__", esc(f"입력별 검토 — {out.name}")).replace("__TABS__", "".join(tabs))
                    .replace("__BODY__", summary + "".join(panes)), encoding="utf-8")
    return path


# ---- 시간 ----

def _fmt(sec) -> str:
    if sec is None:
        return "-"
    return f"{sec:.1f}초" if sec < 60 else f"{int(sec // 60)}분 {sec % 60:.1f}초"


def _sum(t: dict, key: str):
    return sum((t.get(p) or {}).get(key) or 0 for p, _ in PHASES)


def _time_rows(t: dict) -> str:
    rows = ""
    for key, name in PHASES:
        if key not in t:            # 그 실행에 없는 단계
            continue
        x = t.get(key) or {}
        bad = f" <span class='bad'>(실패 {x['failed']})</span>" if x.get("failed") else ""
        if not x.get("calls"):
            rows += f"<tr><td>{name}</td><td class='mute'>-</td><td>0</td><td class='mute'>-</td><td>0</td><td>0</td></tr>"
            continue
        rows += (f"<tr><td>{name}</td><td>{_fmt(x.get('wall_seconds'))}</td><td>{x.get('calls', 0)}{bad}</td>"
                 f"<td>{_fmt(x.get('call_seconds'))}</td><td>{x.get('prompt_tokens', 0):,}</td><td>{x.get('completion_tokens', 0):,}</td></tr>")
    return rows + (f"<tr class='tot'><td>합계</td><td>{_fmt(t.get('total_wall_seconds'))}</td><td>{_sum(t, 'calls')}</td>"
                   f"<td>{_fmt(_sum(t, 'call_seconds'))}</td><td>{_sum(t, 'prompt_tokens'):,}</td><td>{_sum(t, 'completion_tokens'):,}</td></tr>")


def _pipe_block(p: dict, files: list[str]) -> str:
    """입력 → 파싱 → 결과 저장 → 의미 단계 전체 시간(run_pipeline trace/timing.json)."""
    rows = [("OCR·영역 검출(Stage 1)", p.get("ocr_wall_seconds"), ""), ("문구 판독(VLM)", p.get("read_wall_seconds"),
             f"호출 {p['read_http_calls']}회" if p.get("read_http_calls") is not None else ""),
            *([("누락 영역 보충", p.get("supplement_wall_seconds"), p.get("supplement_note") or "")]
              if p.get("supplement_wall_seconds") is not None else []),
            ("파싱 소계", p.get("parse_wall_seconds"), "OCR·판독·보충·결과 조립"), ("결과 저장·화면", p.get("export_wall_seconds"), "layout-result/2·대조 화면"),
            ("의미 단계", p.get("semantic_wall_seconds"), "상품 분류·배정·라벨링(아래 단계별)")]
    shared = f" · 같은 묶음의 파일 {len(files)}개를 함께 처리" if len(files) > 1 else ""
    return (f"<details class='table-checks' open><summary>전체 소요(파싱+의미 단계) {_fmt(p.get('end_to_end_wall_seconds'))}</summary>"
            "<table class='meta'><tr><th>단계</th><th>경과(벽시계)</th><th>비고</th></tr>"
            + "".join(f"<tr><td>{k}</td><td>{_fmt(v)}</td><td class='mute'>{esc(n)}</td></tr>" for k, v, n in rows)
            + f"<tr class='tot'><td>전체</td><td>{_fmt(p.get('end_to_end_wall_seconds'))}</td><td class='mute'>{esc(p.get('note') or '')}</td></tr>"
            + f"</table><div class='note'>시작 {esc(str(p.get('started_at') or '-'))}{shared}. VLM 동시 호출 상한 2(파싱·의미 단계 공유).</div></details>")


def _time_block(sub: str, files: list[str], t: dict | None, rep: dict) -> str:
    if not t:
        return "<div class='note'>이번 실행의 시간 기록이 없다(report.json).</div>"
    replay = (rep.get("timing") or {}).get("replay")
    shared = f" · 같은 묶음의 파일 {len(files)}개({esc(', '.join(files))})를 함께 처리한 시간" if len(files) > 1 else ""
    pipe = _pipe_block(t["pipeline"], files) if t.get("pipeline") else ""
    return pipe + (f"<details class='table-checks' open><summary>소요시간(의미 단계) {_fmt(t.get('total_wall_seconds'))} · 묶음 {esc(sub)}</summary>"
            + ("<div class='warn'>저장 응답으로 재구성한 실행이라 아래 시간은 실제 호출 시간이 아니다.</div>" if replay else "")
            + "<table class='meta'><tr><th>단계</th><th>경과(벽시계)</th><th>호출</th><th>호출 시간 합</th><th>입력 토큰</th><th>출력 토큰</th></tr>"
            + _time_rows(t)
            + f"</table><div class='note'>시간은 입력 묶음({esc(sub)}) 단위 기록이다{shared}. 묶음은 동시에 실행돼 경과 시간에 대기 시간이 섞이고, "
              "파싱 단계(OCR·판독) 시간은 포함되지 않는다.</div></details>")


# ---- 정답 채점 ----

def _tpl_list(g: dict, codes: list[str], hit: set[str], miss_cls: str) -> str:
    if not codes:
        return "<span class=mute>없음</span>"
    return " ".join(_chip(g["names"].get(c, c), "lab" if c in hit else miss_cls, title=c) for c in codes)


def _verdict_chip(g: dict) -> str:
    name, color, _ = VERDICT[g["verdict"]]
    return _chip(name, style=f"background:{color}")


def _gold_cell(g: dict) -> str:
    """요약 표 칸: 판정 + 정답·결과 템플릿(채점 제외면 상품군)."""
    hit = set(g["gold"]) & set(g["predicted"])
    body = (f"정답 {_tpl_list(g, g['gold'], hit, 'bad')}<br>결과 {_tpl_list(g, g['predicted'], hit, 'rev')}"
            if g["verdict"] != "unscored" else f"정답 상품군 {esc(g.get('group') or '-')} · 파싱 품질만")
    return f"{_verdict_chip(g)}<div class='note'>{body}</div>"


def _gold_block(g: dict | None) -> str:
    """입력 화면 위쪽: 파일명에서 읽은 정답 템플릿 대 결과 템플릿(묶음 단위)."""
    if not g:
        return ""
    files = f"<div class='note'>원래 파일명: {esc(' / '.join(g['files']))}</div>"
    if g["verdict"] == "unscored":
        return (f"<div class='gold'>{_verdict_chip(g)} <span class='mute'>정답 상품군 {esc(g.get('group') or '-')}: 템플릿이 정해지지 않아 "
                f"채점하지 않는다(파싱 품질만 본다). 결과 상품군 {esc(', '.join(g['product_groups']) or '-')} · 결과 템플릿 "
                f"{_tpl_list(g, g['predicted'], set(), 'alt')}</span>{files}</div>")
    hit = set(g["gold"]) & set(g["predicted"])
    diff = ([f"빠짐 {', '.join(g['names'].get(c, c) for c in g['missing'])}"] if g["missing"] else []) + (
        [f"더함 {', '.join(g['names'].get(c, c) for c in g['extra'])}"] if g["extra"] else [])
    return (f"<div class='gold'>{_verdict_chip(g)} <span class='mute'>{esc(' · '.join(diff))}</span><table class='meta'>"
            f"<tr><th>정답(파일명)</th><td>{_tpl_list(g, g['gold'], hit, 'bad')}</td></tr>"
            f"<tr><th>결과</th><td>{_tpl_list(g, g['predicted'], hit, 'rev')}</td></tr></table>{files}"
            "<div class='note'>묶음(같은 상품의 여러 입력) 단위로 상품들의 템플릿을 모아 채점한다(퇴직연금 운용상품 제외).</div></div>")


# ---- 영역 ----

def _short(rid: str) -> str:
    p = rid.split("-")
    return "-".join(p[-2:]) if len(p) > 1 and p[-2][:1] == "t" and p[-2][1:].isdigit() else p[-1]


def _num(pid: str) -> str:
    tail = pid.rsplit("_", 1)[-1]
    return str(int(tail)) if tail.isdigit() else pid


def _chip(text, cls="", style="", title="") -> str:
    return (f"<span class='chip {cls}'" + (f" style='{style}'" if style else "") + (f" title='{esc(title)}'" if title else "")
            + f">{esc(text)}</span>")


def _assign(pa: dict, color: dict, prods: dict) -> tuple[str, str]:
    """(영역 상자 색, 상품 칩)."""
    st, ids, scope = pa["status"], pa["product_ids"], pa.get("scope")
    pchips = "".join(_chip(i, style=f"background:{color.get(i, SHARED)}", title=(prods.get(i) or {}).get("name") or "") for i in ids)
    if st == "not_run":
        return NOT_RUN, _chip("상품 배정 미실행", "none")
    if st == "failed":
        return UNKNOWN, _chip("배정 실패", "bad")
    if st == "partial":
        return UNKNOWN, _chip("소속 미확정", "rev")
    auto = _chip("자동", "none", title="후보 상품이 하나라 모델 호출 없이 배정") if pa.get("basis") == "single_product" else ""
    if scope == "shared":
        return SHARED, _chip("공통", style=f"background:{SHARED}") + pchips
    if scope == "mixed":
        return MIXED, _chip("혼합", style=f"background:{MIXED}") + pchips
    return color.get(ids[0], SHARED) if ids else SHARED, pchips + auto


def _labels(lb: dict) -> tuple[list[str], str]:
    items, names = lb["items"], []
    chips = ""
    for it in items:
        pre = f"p{_num(it['product_id'])}·" if len(items) > 1 else ""
        for x in it["label_ids"]:
            names.append(x)
            chips += _chip(pre + x, "lab")
    st = lb["status"]
    if st == "not_run":
        chips += _chip("라벨 미실행", "none")
    elif st == "failed":
        chips += _chip("라벨 실패", "bad")
    else:
        if not names:
            chips += _chip("라벨 없음", "none")
        if st == "partial":
            chips += _chip("라벨 일부", "rev")
    return list(dict.fromkeys(names)), chips


def _cells(t: dict) -> str:
    if t.get("cells"):
        h = "<table class='grid'>"
        for i in range(t["n_rows"]):
            h += "<tr>"
            for c in sorted((c for c in t["cells"] if c["row"] == i), key=lambda c: c["col"]):
                mg = c["rowspan"] > 1 or c["colspan"] > 1
                mark = (f"<sup>{'↕' + str(c['rowspan']) if c['rowspan'] > 1 else ''}{'↔' + str(c['colspan']) if c['colspan'] > 1 else ''}</sup>"
                        if mg else "")
                h += (f"<td rowspan='{c['rowspan']}' colspan='{c['colspan']}' class='{'mg' if mg else '' if c['text'] else 'emp'}'>"
                      f"{esc(c['text']) or '<span class=mute>(빈 칸)</span>'}{mark}</td>")
            h += "</tr>"
        return h + "</table>"
    return "<table class='grid'>" + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in t.get("rows") or []) + "</table>"


def _text(s: str) -> str:
    return esc(s).replace("\n", "<br>")


def _evidence(lt: dict | None) -> str:
    if not lt:
        return ""
    ev = [(pid, e) for pid, es in (lt.get("evidence") or {}).items() for e in es]
    rj = [(pid, e) for pid, es in (lt.get("rejected") or {}).items() for e in es]
    if not (ev or rj):
        return ""
    body = "".join(
        f"<div class='span'><b>{esc(e['label'])}</b> <i>{esc(pid)}</i> “{esc(e['quote'])}”"
        + (f" <i>(표 셀 {esc(' '.join(','.join(map(str, c)) for c in e['where']['cells']))})</i>" if (e.get("where") or {}).get("cells") else "")
        + "</div>" for pid, e in ev)
    body += "".join(f"<div class='span warn'><b>버린 라벨 {esc(e['label'])}</b> <i>{esc(pid)} · {esc(e.get('why', ''))}</i> “{esc(e['quote'])}”</div>"
                    for pid, e in rj)
    return f"<details class='ev'><summary>라벨 근거 인용 {len(ev)} · 버린 라벨 {len(rj)}</summary>{body}</details>"


def _region(seq: int, r: dict, color: dict, prods: dict, lt: dict | None) -> tuple[str, dict]:
    pa, lb = r["product_assignment"], r["labeling"]
    sr = r.get("semantic_review") or {"required": False, "codes": []}
    box, pchips = _assign(pa, color, prods)
    names, lchips = _labels(lb)
    t = r.get("table")
    is_table = bool(t and t["status"] == "table")
    flags = [k for k, on in (("parse", r["review"]["required"]), ("sem", sr["required"]), ("table", is_table),
                             ("nolabel", lb["status"] == "completed" and not names), ("unknown", pa["status"] in ("partial", "failed")),
                             ("fail", r["text"] is None)) if on]
    chips = pchips + lchips
    if is_table:
        chips += _chip("표", "alt")
    elif t and t["status"] == "unknown":
        chips += _chip("표 판정 없음", "rev")
    if r.get("candidate_boundary"):
        chips += _chip("후보 경계(미검증)", "rev")
    if r["review"]["required"]:
        chips += _chip("파싱 검토", "rev")
    if sr["required"]:
        chips += _chip("의미 검토", "sem")
    if r["text"] is None:
        body = f"<div class='warn'>판독 실패 · 최종 문구 없음 ({esc(r['text_status'])})</div>"
    elif is_table and (t.get("cells") or t.get("rows")):
        body = (f"<div class='note'>논리 격자 {t.get('n_rows', len(t.get('rows') or []))}행 × {t.get('n_cols', '')}열 · 주황 칸 = 병합</div>{_cells(t)}"
                f"<details class='ev'><summary>표 전체 문구</summary><div class='text'>{_text(r['text'])}</div></details>")
    else:
        body = f"<div class='text'>{_text(r['text']) or '<span class=mute>(빈 문자열: 글자 없음으로 판독)</span>'}</div>"
    codes = "".join(f"<span class='code {k}'>{esc(c)}</span>" for k, cs in (("q", r["review"]["quality_reasons"]), ("c", r["review"]["context_reasons"]),
                                                                         ("s", sr["codes"])) for c in cs)
    li = (f"<li class='region' id='r-{esc(r['region_id'])}' data-rid='{esc(r['region_id'])}' data-flags='{' '.join(flags)}' "
          f"data-scope='{esc(str(pa.get('scope') or pa['status']))}' data-pids='{esc(' '.join(pa['product_ids']))}'>"
          f"<div class='head'><span class='seq'>{seq}</span><span class='rid'>{esc(_short(r['region_id']))}</span>{chips}</div>{body}"
          + (f"<div class='note'>{codes}</div>" if codes else "") + _evidence(lt) + "</li>")
    return li, {"box": box, "flags": flags, "labels": names}


def _overlay(w: int, h: int, items: list[tuple[int, dict, str]], orphans: list[dict]) -> str:
    sw, fs = round(max(2.0, w / 450), 1), round(max(12.0, w / 55), 1)
    s = f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' class='overlay' style='--sw:{sw}px'>"
    for seq, r, c in items:
        b = r["bbox"]
        dash = f" stroke-dasharray='{sw * 3} {sw * 2}'" if c == UNKNOWN else ""
        s += (f"<g class='box' data-rid='{esc(r['region_id'])}'><rect x='{b[0]:.1f}' y='{b[1]:.1f}' width='{b[2] - b[0]:.1f}' height='{b[3] - b[1]:.1f}' "
              f"stroke='{c}' fill='{c}'{dash}><title>{seq} · {esc(r['region_id'])}</title></rect>"
              f"<text x='{b[0] + 3:.1f}' y='{b[1] + fs:.1f}' fill='{c}' font-size='{fs}'>{seq}</text></g>")
    s += "<g class='orph'>" + "".join(
        f"<rect class='orphan' x='{u['bbox'][0]:.1f}' y='{u['bbox'][1]:.1f}' width='{u['bbox'][2] - u['bbox'][0]:.1f}' "
        f"height='{u['bbox'][3] - u['bbox'][1]:.1f}'><title>영역 밖 OCR({esc(u['reason'])}): {esc(u['ocr_text'])}</title></rect>" for u in orphans) + "</g>"
    return s + "</svg>"


def _image(out: Path, fdir: Path, page: dict) -> str:
    p = Path(os.path.normpath(fdir / page["image"]["path"]))   # '..' 이 섞인 긴 경로(260자 제한) 방지
    if not p.is_file():
        return f"<div class='warn'>페이지 이미지가 없다: {esc(str(p))}</div>"
    return f"<img loading='lazy' alt='{esc(page['page_id'])}' src='{quote(os.path.relpath(p, out).replace(os.sep, '/'), safe='/')}'>"


def _pane(out: Path, fdir: Path, d: dict, siblings: list[str], timing: dict | None, rep: dict,
          gold: dict | None = None) -> tuple[str, dict]:
    fid, sub, src = d["source"]["file_id"], d["submission_id"], d["source"]
    prods = {p["product_id"]: p for p in d.get("products") or []}
    color = {pid: PALETTE[i % len(PALETTE)] for i, pid in enumerate(prods)}
    ref = (d.get("trace_ref") or {}).get("semantic_trace")
    tp = Path(os.path.normpath(fdir / ref)) if ref else None
    ltrace = (load(tp).get("labels") or {}) if tp and tp.is_file() else {}
    orphans: dict[str, list] = {}
    for u in d.get("unassigned_texts") or []:
        if not u["empty_text"]:
            orphans.setdefault(u["page_id"], []).append(u)
    by_page: dict[str, list] = {}
    for seq, r in enumerate(d["regions"], 1):
        by_page.setdefault(r["page_id"], []).append((seq, r))
    left, right, info, last_page = "", "", [], None
    for p in d["pages"]:
        items = []
        for seq, r in by_page.get(p["page_id"], []):
            li, x = _region(seq, r, color, prods, ltrace.get(r["region_id"]))
            if len(d["pages"]) > 1 and p["page_id"] != last_page:
                right += f"<li class='pgsep'>p{p['page_no']} · {esc(p['page_id'])}</li>"
            last_page = p["page_id"]
            right += li
            info.append(x)
            items.append((seq, r, x["box"]))
        warn = (f"<div class='warn top'>{esc(p['execution_status'])}: 이 페이지는 확정 결과가 아니다</div>"
                if p["execution_status"] != "READY" else "")
        left += (f"<div class='pg'><div class='pgh'>p{p['page_no']} · {p['width']}×{p['height']} · {esc(p['execution_status'])}</div>{warn}"
                 f"<div class='canvas'>{_image(out, fdir, p)}{_overlay(p['width'], p['height'], items, orphans.get(p['page_id'], []))}</div></div>")
    n = len(info)
    cnt = lambda k: sum(k in x["flags"] for x in info)  # noqa: E731
    multi = sum(len(x["labels"]) > 1 for x in info)
    n_orph = sum(map(len, orphans.values()))
    stat = lambda v, k, cls="": f"<div class='stat {cls}'><b>{v}</b><span>{k}</span></div>"  # noqa: E731
    stats = "".join([stat(n, "Region"), stat(cnt("table"), "표"), stat(multi, "복수 라벨"), stat(cnt("nolabel"), "라벨 없음"),
                     stat(cnt("parse"), "파싱 검토", "w" if cnt("parse") else ""), stat(cnt("sem"), "의미 검토", "w" if cnt("sem") else ""),
                     stat(cnt("unknown"), "소속 미확정", "w" if cnt("unknown") else ""),
                     stat(cnt("fail"), "판독 실패", "w" if cnt("fail") else ""), stat(n_orph, "영역 밖 OCR")])
    if timing and timing.get("pipeline"):
        stats += stat(_fmt(timing["pipeline"].get("end_to_end_wall_seconds")), "전체 소요(파싱+의미)")
    if timing and timing.get("total_wall_seconds") is not None:
        stats += stat(_fmt(timing.get("total_wall_seconds")), f"의미 단계 소요 · {esc(sub)}")
    mine = [p for p in prods.values() if fid in p.get("files", [])]
    ptab = ""
    if mine:
        ptab = "<table class='meta'><tr><th>상품</th><th>이름</th><th>상태</th><th>상품군</th><th>템플릿</th><th>Region</th><th>검토</th></tr>"
        for p in mine:
            tpl = "<br>".join(esc(t["template_id"]) + (" <span class=mute>(운용상품)</span>" if t.get("role") == "component" else "")
                              for t in p["templates"]) or "<span class=mute>템플릿 없음</span>"
            pen = ""
            if p.get("pension"):
                q = p["pension"]
                pen = (f"<div class='note'>퇴직연금 금융투자상품 {PENSION.get(q['investment_products'], '')} · 제도 {esc('/'.join(q['plans']) or '-')}"
                       f" · 운용상품 {esc('/'.join(q['components']) or '-')}</div>")
            cntp = sum(p["product_id"] in r["product_assignment"]["product_ids"] for r in d["regions"])
            pcodes = "".join(f"<span class='code s'>{esc(c)}</span>" for c in p["review"]["codes"]) or "<span class=mute>-</span>"
            ptab += (f"<tr><td>{_chip(p['product_id'], style='background:' + color[p['product_id']])}</td><td>{esc(p['name'] or '(이름 없음)')}</td>"
                     f"<td>{PSTATUS.get(p['status'], '')}</td><td>{esc(p['product_group'] or '')}</td><td>{tpl}{pen}</td>"
                     f"<td>{cntp}개</td><td>{pcodes}</td></tr>")
        ptab += "</table>"
    issues = [(p["page_id"], i) for p in d["pages"] for i in p.get("issues", [])]
    itab = ("<details class='table-checks'><summary>페이지 이슈 %d</summary><ol>%s</ol></details>" % (
        len(issues), "".join(f"<li><b>{esc(i['code'])}</b> <span class=mute>{esc(pid)}</span><div>{esc(i['message'])}</div></li>" for pid, i in issues))
        if issues else "")
    otab = ""
    if n_orph:
        otab = (f"<details class='table-checks'><summary>영역 밖 OCR {n_orph}건 · 최종 문구 아님</summary><ul class='orphans'>"
                + "".join(f"<li class='orphan-row'>{esc(u['reason'])}: {esc(u['ocr_text'])}</li>" for us in orphans.values() for u in us) + "</ul></details>")
    scopes = {r["product_assignment"].get("scope") for r in d["regions"]} - {None}
    opts = "<option value='all'>상품 전체</option>" + "".join(
        f"<option value='{esc(pid)}'>{esc(pid)} {esc(p['name'] or '')}</option>" for pid, p in prods.items() if fid in p.get("files", [])) + "".join(
        f"<option value='scope:{k}'>{lab} 영역</option>" for k, lab in (("shared", "공통"), ("mixed", "혼합")) if k in scopes)
    flt = "".join(f"<label><input type='radio' name='f-{esc(fid)}' value='{k}'{' checked' if k == 'all' else ''}>{lab}"
                  f"{'' if k == 'all' else f' {cnt(k)}'}</label>" for k, lab in (("all", "전체"), ("parse", "파싱 검토"), ("sem", "의미 검토"),
                                                                           ("table", "표"), ("nolabel", "라벨 없음"), ("unknown", "소속 미확정"),
                                                                           ("fail", "판독 실패")))
    label = f"{src['input_order']}. {src['file_name']}"
    pane = (f"<section class='pane' data-key='{esc(fid)}'><h2>{esc(fid)} · {esc(src['file_name'])}</h2>"
            f"<div class='mute sub'>{esc(sub)} · {esc(src['file_type'])} · {len(d['pages'])}쪽 · 입력 순서 {src['input_order']}</div>"
            f"{_gold_block(gold)}<div class='stats'>{stats}</div>{_time_block(sub, siblings, timing, rep)}{ptab}{itab}{otab}"
            f"<div class='split'><div class='left'>{left}</div><div class='right'>"
            f"<div class='filters'>{flt} <select class='pf'>{opts}</select> <label><input type='checkbox' class='orph-toggle'>영역 밖 OCR 표시</label> "
            f"<span class='mute fcount'></span></div><ol class='regions'>{right}</ol></div></div></section>")
    return pane, {"fid": fid, "sub": sub, "label": f"{fid} · {src['file_name']}", "name": src["file_name"], "pages": len(d["pages"]),
                  "regions": n, "products": len(mine), "tables": cnt("table"), "nolabel": cnt("nolabel"), "parse": cnt("parse"), "sem": cnt("sem"),
                  "unknown": cnt("unknown"), "fail": cnt("fail"), "timing": timing, "verdict": (gold or {}).get("verdict")}


def _summary(out: Path, rows: list[dict], sub_timing: dict, rep: dict, rep_path: str | None, run: dict, index: dict,
             gold: dict | None = None) -> str:
    gold = gold or {}
    wall = rep.get("timing") or {}
    totals = {s: (t or {}).get("total_wall_seconds") for s, t in sub_timing.items()}
    peak = max([v for v in totals.values() if v] or [0])
    stat = lambda v, k: f"<div class='stat'><b>{v}</b><span>{k}</span></div>"  # noqa: E731
    stats = [stat(len(rows), "파일(입력)"), stat(len({r["sub"] for r in rows}), "입력 묶음"), stat(sum(r["regions"] for r in rows), "Region"),
             stat(sum(r["parse"] for r in rows), "파싱 검토"), stat(sum(r["sem"] for r in rows), "의미 검토")]
    if gold:
        n = {k: sum(g["verdict"] == k for g in gold.values()) for k in VERDICT}
        stats = [stat(f"{n['correct']} / {n['correct'] + n['partial'] + n['wrong']}", "템플릿 정답(묶음)"), stat(n["partial"], "부분"),
                 stat(n["wrong"], "틀림"), stat(n["unscored"], "채점 제외")] + stats
    pipes = {r["sub"]: (r["timing"] or {}).get("pipeline") for r in rows if (r["timing"] or {}).get("pipeline")}
    if pipes:
        stats.append(stat(_fmt(sum(p.get("end_to_end_wall_seconds") or 0 for p in pipes.values())), "전체 소요 합(파싱+의미, 입력별)"))
    if wall:
        stats += [stat(_fmt(wall.get("wall_seconds")), "의미 단계 전체 소요"), stat(wall.get("http_calls", "-"), "의미 단계 모델 호출")]
    e2e_peak = max([p.get("end_to_end_wall_seconds") or 0 for p in pipes.values()] or [0])
    body = ""
    last = None
    for r in rows:
        group = [x for x in rows if x["sub"] == r["sub"]]
        tcells = ""
        if r["sub"] != last:
            n, t = len(group), r["timing"]
            if gold:
                g = gold.get(r["sub"])
                tcells += f"<td rowspan='{n}' class='gd'>{_gold_cell(g)}</td>" if g else f"<td rowspan='{n}' class='mute'>-</td>"
            if pipes:
                p = pipes.get(r["sub"])
                tcells += (f"<td rowspan='{n}' class='tm'><b>{_fmt(p.get('end_to_end_wall_seconds'))}</b><div class='bar-t'><i style='width:"
                           f"{100 * (p.get('end_to_end_wall_seconds') or 0) / e2e_peak if e2e_peak else 0:.0f}%'></i></div><span class='mute'>"
                           f"OCR {_fmt(p.get('ocr_wall_seconds'))} · 판독 {_fmt(p.get('read_wall_seconds'))}"
                           + (f" · 보충 {_fmt(p['supplement_wall_seconds'])}" if p.get("supplement_wall_seconds") is not None else "")
                           + f" · 저장 {_fmt(p.get('export_wall_seconds'))}"
                           f" · 의미 {_fmt(p.get('semantic_wall_seconds'))}</span></td>"
                           if p else f"<td rowspan='{n}' class='mute'>-</td>")
            if t and t.get("total_wall_seconds") is not None:
                pct = 100 * (t.get("total_wall_seconds") or 0) / peak if peak else 0
                parts = " · ".join(f"{name.split('(')[0]} {_fmt((t.get(k) or {}).get('wall_seconds')) if (t.get(k) or {}).get('calls') else '-'}"
                                   for k, name in PHASES if k in t)
                bad = _sum(t, "failed")
                tcells += (f"<td rowspan='{n}'>{esc(r['sub'])}</td><td rowspan='{n}' class='tm'><b>{_fmt(t.get('total_wall_seconds'))}</b>"
                           f"<div class='bar-t'><i style='width:{pct:.0f}%'></i></div><span class='mute'>{parts}</span></td>"
                           f"<td rowspan='{n}'>{_sum(t, 'calls')}{f' <span class=bad>(실패 {bad})</span>' if bad else ''}</td>")
            else:
                tcells += f"<td rowspan='{n}'>{esc(r['sub'])}</td><td rowspan='{n}' colspan='2' class='mute'>시간 기록 없음</td>"
        last = r["sub"]
        w = lambda k: f"<td class='{'wc' if r[k] else ''}'>{r[k]}</td>"  # noqa: E731
        body += (f"<tr><td><a href='#{esc(r['fid'])}'>{esc(r['label'])}</a></td><td>{r['pages']}</td><td>{r['regions']}</td><td>{r['products']}</td>"
                 f"<td>{r['tables']}</td><td>{r['nolabel']}</td>{w('parse')}{w('sem')}{w('unknown')}{w('fail')}{tcells}</tr>")
    meta = [("계약", f"{esc(index.get('contract', ''))} · 프로필 {esc(str(rep.get('semantic_profile', '-')))}"
                    f" · 자산 {esc(str((rep.get('versions') or {}).get('assets', '-')))}")]
    if run.get("started_at"):
        meta.append(("실행 시작", esc(run["started_at"])))
    if wall:
        meta.append(("동시 호출", f"상한 {wall.get('concurrency_limit')} · 실제 최대 {wall.get('peak_concurrency')} · 모델 호출 {wall.get('http_calls')}회"))
    if rep.get("parse_dir"):
        meta.append(("근거 파싱 결과", esc(rep["parse_dir"])))
    if run.get("pipeline"):
        q = run["pipeline"]
        meta.append(("파이프라인(실행 전체)", f"전체 {_fmt(q.get('end_to_end_wall_seconds'))} · OCR {_fmt(q.get('ocr_wall_seconds'))} · 판독 "
                                       f"{_fmt(q.get('read_wall_seconds'))} · 의미 {_fmt(q.get('semantic_wall_seconds'))} "
                                       "<span class=mute>(여러 묶음을 한 번에 실행해 입력별로 나눌 수 없음)</span>"))
    ck = rep.get("checks")
    if ck:
        meta.append(("계약·보존 검사", f"{'통과' if ck.get('passed') else '<span class=bad>실패</span>'} · 문서 {ck.get('documents')} · 문제 {ck.get('problems')}"
                                  " <span class=mute>(구조·참조·보존·인용 위치 검사이며 분류·라벨 내용 정확도 검사가 아님)</span>"))
    notes = ""
    if not rep:
        notes = "<div class='warn'>report.json 이 없어 시간을 알 수 없다.</div>"
    elif rep_path == "report.rebuild.json" or wall.get("replay"):
        notes = "<div class='warn'>저장 응답으로 재구성한 기록(report.rebuild.json)이라 아래 시간은 실제 호출 시간이 아니다.</div>"
    elif pipes:
        notes = ("<div class='note'>전체 소요는 입력(묶음)마다 run_pipeline.py 기본 프로필로 따로 실행해 잰 벽시계 시간이다"
                 "(OCR·판독·결과 저장·의미 단계 포함, 입력끼리 겹치지 않음). 의미 단계 열은 그중 의미 단계 부분이다. "
                 "같은 묶음의 파일은 한 번에 처리해 시간을 파일별로 나눌 수 없다.</div>")
    else:
        done = [v for v in totals.values() if v]
        notes = (f"<div class='note'>묶음은 동시에 실행돼 서로 겹친다(묶음별 경과 합 {_fmt(sum(done))} / 전체 {_fmt(wall.get('wall_seconds'))}). "
                 "경과 시간에는 모델 호출 대기 시간이 섞여 있다. 같은 묶음의 파일은 한 번에 처리해 시간을 나눌 수 없다. "
                 + ("파싱 시간은 위 '파이프라인(실행 전체)'에 있다.</div>" if run.get("pipeline") else
                    "파싱 단계(OCR·판독)는 저장된 결과를 읽기만 해서 이 시간에 포함되지 않는다.</div>"))
    if gold:
        meta.append(("정답", "파일명에서 읽은 템플릿(실행 중에는 파일명을 모델에 주지 않음) · 묶음 단위 채점 · 정답 = 결과 템플릿 집합이 같음, "
                             "부분 = 겹치지만 빠지거나 더함, 틀림 = 안 겹침, 채점 제외 = 템플릿 미정(카드 등, 파싱 품질만)"))
    legend = ("<details class='table-checks'><summary>보는 법</summary><div class='note'>상자 색 — 상품별 색(파일 화면의 상품 표와 같음) · "
              f"<span class='dot' style='background:{SHARED}'></span>공통 · <span class='dot' style='background:{MIXED}'></span>혼합 · "
              f"<span class='dot' style='background:{UNKNOWN}'></span>소속 미확정·배정 실패(점선) · 상자 안 숫자는 오른쪽 목록 번호. "
              "상자와 목록 항목은 서로 눌러 찾는다. '영역 밖 OCR 표시'를 켜면 어느 영역에도 들어가지 않은 OCR(최종 문구 아님)이 빨간 점선으로 보인다.</div></details>")
    return ("<section class='pane' data-key='summary'><h2>전체 요약</h2><div class='stats'>" + "".join(stats) + "</div>"
            "<table class='meta'>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in meta) + "</table>" + legend
            + "<table class='meta sum'><tr><th>파일</th><th>쪽</th><th>Region</th><th>상품</th><th>표</th><th>라벨 없음</th><th>파싱 검토</th><th>의미 검토</th>"
              "<th>소속 미확정</th><th>판독 실패</th>" + ("<th>템플릿 채점(묶음)</th>" if gold else "")
            + ("<th>전체 소요(파싱+의미)</th>" if pipes else "")
            + "<th>묶음</th><th>의미 단계 소요(묶음)</th><th>호출</th></tr>" + body + "</table>" + notes + "</section>")


_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --line:#d8dde3; --bg:#f6f7f9; --ink:#1c2430; --muted:#68727f; }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.6 -apple-system,"Segoe UI","Malgun Gothic",sans-serif; color:var(--ink); background:var(--bg); }
header { position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid var(--line); padding:10px 16px; }
h1 { font-size:15px; margin:0; flex:none; }
h2 { font-size:15px; margin:0 0 2px; }
.bar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
/* 입력이 수십 건이면 탭 목록만으로 화면 절반을 덮는다. 기본은 접고 지금 보는 입력 이름을 버튼에 띄운다. */
.toggle { font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line); background:#fff; border-radius:6px; cursor:pointer;
  color:var(--ink); display:flex; align-items:center; gap:8px; max-width:min(70vw, 720px); }
.toggle:hover { border-color:#9aa5b1; }
.toggle .who { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.toggle .count { color:var(--muted); flex:none; }
.toggle .caret { color:var(--muted); flex:none; transition:transform .15s; }
.toggle[aria-expanded="true"] .caret { transform:rotate(180deg); }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; max-height:42vh; overflow:auto; }
.tabs[hidden] { display:none; }
.tab { font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line); background:#fff; border-radius:6px; cursor:pointer; color:var(--muted); }
.tab:hover { border-color:#9aa5b1; }
.tab.on { background:var(--ink); color:#fff; border-color:var(--ink); }
.tab small { opacity:.7; }
.pane { display:none; padding:12px 16px 40px; }
.pane.on { display:block; }
.sub { font-size:12px; margin-bottom:8px; }
.stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
.stat { background:#fff; border:1px solid var(--line); border-radius:6px; padding:6px 12px; min-width:82px; }
.stat b { display:block; font-size:17px; }
.stat span { font-size:11px; color:var(--muted); }
.stat.w b { color:#EF6C00; }
table.meta { border-collapse:collapse; background:#fff; font-size:12px; margin-bottom:10px; }
table.meta th, table.meta td { border:1px solid var(--line); padding:4px 10px; text-align:left; vertical-align:top; }
table.meta th { background:#eef1f4; font-weight:600; }
table.meta tr.tot td { font-weight:600; background:#fafbfc; }
table.sum td.wc { color:#EF6C00; font-weight:700; }
table.sum td.tm { min-width:230px; }
.bar-t { height:6px; background:#eef1f4; border-radius:3px; margin:3px 0; }
.bar-t i { display:block; height:100%; background:#1E88E5; border-radius:3px; }
.dot { display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:middle; margin:0 3px 0 6px; }
.split { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:14px; align-items:start; }
/* 자체 스크롤을 준다. 페이지가 세로로 길면 sticky 만으로는 아래쪽 Region 이 화면 밖으로 잘린다. 머리글 높이는 JS 가 --hh 로 알려 준다. */
.left { position:sticky; top:calc(var(--hh,86px) + 10px); max-height:calc(100vh - var(--hh,86px) - 24px); overflow:auto; scrollbar-width:thin; }
.pg { margin-bottom:10px; }
.pgh { font-size:12px; color:var(--muted); margin-bottom:2px; }
.canvas { position:relative; background:#fff; border:1px solid var(--line); border-radius:6px; overflow:hidden; }
.canvas img { display:block; width:100%; }
svg.overlay { position:absolute; inset:0; width:100%; height:100%; --sw:4px; }
svg.overlay rect { fill-opacity:.06; stroke-width:var(--sw); }
svg.overlay text { font-weight:700; paint-order:stroke; stroke:#fff; stroke-width:calc(var(--sw)*.75); }
svg.overlay .box { cursor:pointer; }
svg.overlay .box.dim { opacity:.12; }
svg.overlay .box.hot rect { fill-opacity:.3; stroke-width:calc(var(--sw)*2.2); }
svg.overlay .box.sel rect { fill-opacity:.34; stroke-width:calc(var(--sw)*3); }
svg.overlay .orph { display:none; pointer-events:none; }
.pane.show-orph svg.overlay .orph { display:inline; }
svg.overlay rect.orphan { fill:#00000000; stroke:#C62828; stroke-width:calc(var(--sw)*.7); stroke-dasharray:10 7; }
ol.regions { list-style:none; margin:0; padding:0; }
.region { background:#fff; border:1px solid var(--line); border-radius:6px; padding:8px 10px; margin-bottom:6px; scroll-margin-top:calc(var(--hh,86px) + 14px); cursor:pointer; }
.region[hidden] { display:none; }
.region.hot { border-color:var(--ink); box-shadow:0 0 0 2px rgba(28,36,48,.14); }
.region.sel { border-color:#EF6C00; box-shadow:0 0 0 2px rgba(239,108,0,.28); }
.pgsep { font-size:12px; color:var(--muted); margin:10px 0 4px; font-weight:600; }
.head { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:4px; }
.seq { min-width:22px; height:22px; padding:0 5px; border-radius:11px; background:var(--ink); color:#fff; font-size:11px; display:grid; place-items:center; }
.rid { font:11px ui-monospace,Menlo,Consolas,monospace; color:var(--muted); }
.chip { font-size:11px; color:#fff; border-radius:4px; padding:1px 7px; }
.chip.lab { background:#2E7D32; }
.chip.none { background:#fff; color:#9aa5b1; border:1px dashed #c3cbd4; }
.chip.alt { background:#455A64; }
.chip.rev { background:#EF6C00; }
.chip.bad { background:#C62828; }
.chip.sem { background:#00796b; }
.code { display:inline-block; font-size:11px; border:1px solid var(--line); border-radius:4px; padding:0 5px; margin:1px 3px 1px 0; }
.code.q { border-color:#EF6C00; color:#EF6C00; }
.code.c { border-color:#6A1B9A; color:#6A1B9A; }
.code.s { border-color:#00796b; color:#00796b; }
.text { white-space:pre-wrap; word-break:break-word; }
table.grid { border-collapse:collapse; width:100%; font-size:12px; margin:4px 0; }
table.grid td { border:1px solid var(--line); padding:2px 5px; vertical-align:top; word-break:break-word; }
table.grid td.mg { background:#fff3e0; }
table.grid td.mg sup { color:#EF6C00; margin-left:3px; }
table.grid td.emp { background:#fafafa; }
.span { border-left:3px solid var(--line); padding-left:8px; margin:4px 0; font-size:12px; }
.span b { color:#2E7D32; }
.span i, .mute { color:var(--muted); font-style:normal; }
.note { font-size:11px; color:var(--muted); margin-top:4px; }
details.ev { margin-top:4px; font-size:12px; }
details.ev summary { cursor:pointer; color:var(--muted); }
.filters { font-size:12px; background:#fff; border:1px solid var(--line); border-radius:6px; padding:6px 10px; margin-bottom:8px; }
.filters label { margin-right:10px; white-space:nowrap; }
.table-checks { background:#fff; border:1px solid var(--line); border-radius:6px; padding:8px 12px; margin:8px 0; }
.table-checks summary { cursor:pointer; font-weight:600; }
.table-checks ol { margin:8px 0 0; padding-left:24px; }
.table-checks li { padding:4px 0; border-top:1px solid var(--line); }
.warn, .bad { color:#B71C1C; font-size:12px; }
.gold { background:#fff; border:1px solid var(--line); border-radius:6px; padding:8px 12px; margin:6px 0 8px; }
.gold table.meta { margin:6px 0 0; }
table.sum td.gd { min-width:200px; }
table.sum td.gd .chip { display:inline-block; margin:1px 2px 1px 0; }
.warn.top { padding:8px 16px; background:#FFEBEE; }
ul.orphans { margin:8px 0 0; padding-left:18px; }
.orphan-row { font-size:12px; color:#B71C1C; }
@media (max-width:900px) { .split { grid-template-columns:1fr; } .left { position:static; max-height:none; } }
</style></head><body>
<header>
<div class="bar"><h1>__TITLE__</h1>
<button type="button" id="toggle" class="toggle" aria-expanded="false" aria-controls="tabs">
<span class="who" id="current">입력 선택</span>
<span class="count" id="count"></span><span class="caret">&#9662;</span></button></div>
<div class="tabs" id="tabs" hidden>__TABS__</div></header>
__BODY__
<script>
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const tabs = $$('.tab'), panes = $$('.pane'), tabsBox = $('#tabs'), toggle = $('#toggle'), current = $('#current'), hdr = $('header');
$('#count').textContent = (tabs.length - 1) + '개 입력';
const syncHH = () => document.documentElement.style.setProperty('--hh', hdr.offsetHeight + 'px');
new ResizeObserver(syncHH).observe(hdr); syncHH();

function setOpen(open) { tabsBox.hidden = !open; toggle.setAttribute('aria-expanded', open ? 'true' : 'false'); }
function show(key) {
  tabs.forEach(t => t.classList.toggle('on', t.dataset.key === key));
  panes.forEach(p => p.classList.toggle('on', p.dataset.key === key));
  const active = tabs.find(t => t.dataset.key === key);
  if (active) current.textContent = active.dataset.label;
  window.scrollTo(0, 0);
}
// 탭을 고르면 목록은 닫는다 — 고르자마자 내용이 보여야 한다.
tabs.forEach(t => t.addEventListener('click', () => { show(t.dataset.key); history.replaceState(null, '', '#' + t.dataset.key); setOpen(false); }));
toggle.addEventListener('click', () => { const opening = tabsBox.hidden; setOpen(opening); if (opening) $('.tab.on', tabsBox)?.scrollIntoView({block: 'nearest'}); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false); });
setOpen(tabs.length <= 13);

// #f01, #f01_p01, #f01/<region_id>, #summary
function route() {
  const [k, rid] = decodeURIComponent(location.hash.slice(1)).split('/');
  const has = key => tabs.some(t => t.dataset.key === key);
  const key = has(k) ? k : has((k || '').split('_p')[0]) ? k.split('_p')[0] : 'summary';
  show(key);
  if (rid) $(`.pane.on li[data-rid="${CSS.escape(rid)}"]`)?.click();
}
addEventListener('hashchange', route);

function applyFilter(root) {
  const f = $('input[type=radio]:checked', root)?.value || 'all', p = $('.pf', root)?.value || 'all';
  const lis = $$('li.region', root);
  let n = 0;
  lis.forEach(li => {
    const okF = f === 'all' || (li.dataset.flags || '').split(' ').includes(f);
    const okP = p === 'all' || (p.startsWith('scope:') ? li.dataset.scope === p.slice(6) : (li.dataset.pids || '').split(' ').includes(p));
    const ok = okF && okP;
    li.hidden = !ok; if (ok) n++;
    $$(`.left [data-rid="${CSS.escape(li.dataset.rid)}"]`, root).forEach(b => b.classList.toggle('dim', !ok));
  });
  const c = $('.fcount', root); if (c) c.textContent = n + ' / ' + lis.length;
}

// 박스와 오른쪽 항목을 양방향으로 연결한다. 누른 쪽이 아니라 반대쪽을 움직인다.
function link(root) {
  const left = $('.left', root);
  if (!left) return;
  const mark = (rid, cls, on) => $$(`[data-rid="${CSS.escape(rid)}"]`, root).forEach(el => el.classList.toggle(cls, on));
  // 왼쪽은 자체 스크롤이라 창을 건드리지 않고 패널 안에서만 옮긴다(scrollIntoView 는 sticky 로 고정한 패널까지 흔든다).
  const revealBox = box => {
    if (!box) return;
    const b = box.getBoundingClientRect(), p = left.getBoundingClientRect();
    left.scrollTo({top: left.scrollTop + (b.top - p.top) - (p.height - b.height) / 2, behavior: 'smooth'});
  };
  let chosen = null;
  $$('[data-rid]', root).forEach(el => {
    el.addEventListener('mouseenter', () => mark(el.dataset.rid, 'hot', true));
    el.addEventListener('mouseleave', () => mark(el.dataset.rid, 'hot', false));
    el.addEventListener('click', () => {
      const rid = el.dataset.rid;
      if (chosen) mark(chosen, 'sel', false);
      mark(rid, 'sel', true); chosen = rid;
      if (el.closest('.left')) $(`li[data-rid="${CSS.escape(rid)}"]`, root)?.scrollIntoView({behavior: 'smooth', block: 'center'});
      else revealBox($(`.left [data-rid="${CSS.escape(rid)}"]`, root));
    });
  });
  $$('.filters input[type=radio], .pf', root).forEach(el => el.addEventListener('change', () => applyFilter(root)));
  $('.orph-toggle', root)?.addEventListener('change', e => root.classList.toggle('show-orph', e.target.checked));
  applyFilter(root);
}
panes.forEach(link);
route();
</script></body></html>
"""
