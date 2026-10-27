"""최종 결과 대조 화면(정적 HTML). 기본 화면은 Region 별 최종 결과(VLM 최종 문구·판독 상태·검토 근거)다.

출처를 섞지 않는다.
- VLM 최종 문구·표 구조: result.json(사용자 결과). table-cells/1 은 셀 격자(병합 rowspan·colspan)를 그대로 그린다.
- 품질 검토 근거 / 맥락 검토 근거(영역 겹침·경계 걸침·겹친 영역 중복 문구 등): result.json review.
- OCR 참고 원문(배정·경계 걸친 미배정, 원래 OCR 순서): Stage 1 기록. 최종 문구가 아니다.
- 미배정 OCR: result.json unassigned_texts(OCR 원문, 최종 문구 아님).
- 평가용 정답 대조: <out>/eval/eval.json(있을 때만). 사용자 결과가 아니다.
OCR·정답 레이어는 꺼진 상태로 시작하고, 켜야 화면과 옆 목록에 나타난다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .jsonio import load
from .read_stage import read_path
from .vlm_tasks import region_context


def build_final_viewer(out: Path, stage1_dir: Path) -> Path:
    result = load(out / "result.json")
    ev = load(out / "eval" / "eval.json") if (out / "eval" / "eval.json").is_file() else None
    traces = load(out / "trace" / "reads.json") if (out / "trace" / "reads.json").is_file() else {}
    vdir = out / "viewer"
    vdir.mkdir(parents=True, exist_ok=True)
    regions_by_page: dict[str, list] = {}
    for r in result["regions"]:
        regions_by_page.setdefault(r["page_id"], []).append(r)
    un_by_page: dict[str, list] = {}
    for u in result["unassigned_texts"]:
        un_by_page.setdefault(u["page_id"], []).append(u)
    pages = []
    for p in result["pages"]:
        rec = load(stage1_dir / "records" / f"{p['page_id']}.json")
        src = {r["region_id"]: r for r in rec["regions"]}
        page_eval = (ev or {}).get("pages", {}).get(p["page_id"])
        reg_eval = (ev or {}).get("regions", {})
        regs = []
        for r in regions_by_page.get(p["page_id"], []):
            ctx = region_context(rec, src[r["region_id"]])
            e = reg_eval.get(r["region_id"]) or {}
            tr = (traces.get(r["region_id"]) or {}).get("trace") or {}
            regs.append({"id": r["region_id"], "bbox": r["bbox"], "text": r["text"], "status": r["text_status"],
                         "table": r["table"], "review": r["review"], "needs_review": r["needs_review"],
                         "ocr": [{"text": c["text"], "src": c["source"]} for c in ctx["ocr_candidates"]],
                         "calls": r.get("trace_calls"), "label": src[r["region_id"]]["raw_label"],
                         "read_path": r.get("read_path"), "page_text": tr.get("page_text"),
                         "verify_reasons": tr.get("verify_reasons"), "candidate": bool(r.get("candidate_boundary")),
                         "page_view": tr.get("page_view"), "patch_applied": tr.get("patch_applied"),
                         "patch_not_applied": tr.get("patch_not_applied"),
                         "reread_source": tr.get("reread_source"), "reread_text": tr.get("reread_text"),
                         "reread_changes": tr.get("reread_changes"), "reread_notes": tr.get("reread_notes"),
                         "page_rows": tr.get("page_rows"), "table_notes": tr.get("table_notes"),
                         "table_cells_errors": tr.get("table_cells_errors"),
                         "eval": {k: e.get(k) for k in ("missing_lines", "added_segments", "location_error_lines",
                                                        "lines_exact", "lines_approx_only", "expected_lines",
                                                        "unsupported_chars", "missing_digits", "extra_digits")} if e else None})
        pages.append({
            "id": p["page_id"], "w": p["width"], "h": p["height"], "src": p.get("source_name"),
            "sub": p.get("submission_id"), "file": p.get("file_id"), "page_no": p.get("page_no"),
            "img": os.path.relpath(rec["page"]["image_path"], vdir).replace("\\", "/"),
            "exec": p["execution_status"], "quality": p["quality_status"], "reading": p["reading"],
            "basis": rec["route"]["basis"], "plan": rec["route"].get("boundary_plan"),
            "tiles": [{"id": v["view_id"], "crop": v["crop"], "aspect": v["aspect"]} for v in rec["route"]["views"]
                      if v["kind"] != "whole"],
            "regions": regs,
            "overlaps": [{"a": o["a"], "b": o["b"], "box": o["box"], "iou": o["iou"]} for o in rec["diagnostics"]["region_overlaps"]],
            "assigned": [{"bbox": a["ocr_bbox"], "text": a["ocr_text"], "region": a["region_id"]}
                         for a in rec["assignments"] if a["ocr_text"].strip()],
            "unassigned": [{"id": u["ocr_id"], "bbox": u["ocr_bbox"], "text": u["ocr_text"], "reason": u["reason"],
                            "boundary": bool(u["candidate_region_ids"])}
                           for u in un_by_page.get(p["page_id"], []) if not u["empty_text"]],
            "gold": [{"id": g["gold_id"], "text": g["text"], "status": g["status"], "bbox": g.get("bbox"),
                      "geometry": g["geometry"], "region": g.get("region_id")}
                     for g in (page_eval or {}).get("lines", [])],
            "page_eval": {k: v for k, v in (page_eval or {}).items() if k != "lines"} or None,
            "issues": [i for i in result["issues"] if i["page_id"] == p["page_id"]],
        })
    data = {"settings": result["settings"], "checks": result["contract_checks"], "pages": pages,
            "has_eval": ev is not None}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    path = vdir / "index.html"
    path.write_text(_HTML.replace("__DATA__", blob), encoding="utf-8")
    return path


def build_file_viewer(out: Path, docs: list[dict], checks: dict) -> Path:
    """파일별 결과(layout-result/2) 목록으로 만든 대조 화면 → <out>/files/index.html(목록 files/index.json 옆).
    파일·입력 순서별로 페이지를 묶고, 영역마다 상품 배정·라벨링 상태(이번 단계는 미실행)를 함께 보인다.
    문구·표·검토 사유는 result.json, OCR 참고·판독 경로는 trace·Stage 1 기록에서 읽는다."""
    vdir = out / "files"
    vdir.mkdir(parents=True, exist_ok=True)
    pages = []
    for d in sorted(docs, key=lambda d: (d["submission_id"], d["source"]["input_order"])):
        fdir = out / "files" / d["source"]["file_id"]
        reads_path = Path(os.path.normpath(fdir / d["trace_ref"]["reads"]))   # '..' 이 섞인 긴 경로(260자 제한) 방지
        traces = load(reads_path) if reads_path.is_file() else {}
        for p, rec_rel in zip(d["pages"], d["trace_ref"]["stage1_records"]):
            rec = load(os.path.normpath(fdir / rec_rel))
            src = {r["region_id"]: r for r in rec["regions"]}
            regs = []
            for r in (x for x in d["regions"] if x["page_id"] == p["page_id"]):
                ctx = region_context(rec, src[r["region_id"]])
                t = traces.get(r["region_id"]) or {}
                tr = t.get("trace") or {}
                regs.append({"id": r["region_id"], "bbox": r["bbox"], "text": r["text"], "status": r["text_status"],
                             "table": r["table"], "needs_review": r["review"]["required"],
                             "review": {"quality": r["review"]["quality_reasons"], "context": r["review"]["context_reasons"],
                                        "measures": (t.get("review") or {}).get("measures") or {}},
                             "ocr": [{"text": c["text"], "src": c["source"]} for c in ctx["ocr_candidates"]],
                             "calls": tr.get("calls"), "label": src[r["region_id"]]["raw_label"],
                             "read_path": read_path(tr) if tr else None, "page_text": tr.get("page_text"),
                             "verify_reasons": tr.get("verify_reasons"), "candidate": r["candidate_boundary"],
                             "page_view": tr.get("page_view"), "reread_source": tr.get("reread_source"),
                             "reread_text": tr.get("reread_text"), "reread_changes": tr.get("reread_changes"),
                             "reread_notes": tr.get("reread_notes"), "page_rows": tr.get("page_rows"),
                             "table_notes": tr.get("table_notes"), "table_cells_errors": tr.get("table_cells_errors"),
                             "pa": r["product_assignment"], "lb": r["labeling"], "eval": None})
            rs = [x for x in d["regions"] if x["page_id"] == p["page_id"]]
            pages.append({
                "id": p["page_id"], "w": p["width"], "h": p["height"], "src": d["source"]["file_name"],
                "sub": d["submission_id"], "file": d["source"]["file_id"], "page_no": p["page_no"],
                "order": d["source"]["input_order"], "ftype": d["source"]["file_type"], "fpages": len(d["pages"]),
                "result": os.path.relpath(fdir / "result.json", vdir).replace("\\", "/"),
                "img": os.path.relpath(os.path.normpath(fdir / p["image"]["path"]), vdir).replace("\\", "/"),
                "exec": p["execution_status"], "quality": p["quality_status"],
                "reading": None if not rs else {"regions": len(rs), "read_failed": sum(x["text_status"] == "read_failed" for x in rs),
                                                "not_read": sum(x["text_status"] == "not_read" for x in rs),
                                                "needs_review_quality": sum(bool(x["review"]["quality_reasons"]) for x in rs)},
                "basis": rec["route"]["basis"], "plan": rec["route"].get("boundary_plan"),
                "tiles": [{"id": v["view_id"], "crop": v["crop"], "aspect": v["aspect"]} for v in rec["route"]["views"]
                          if v["kind"] != "whole"],
                "regions": regs,
                "overlaps": [{"a": o["a"], "b": o["b"], "box": o["box"], "iou": o["iou"]} for o in rec["diagnostics"]["region_overlaps"]],
                "assigned": [{"bbox": a["ocr_bbox"], "text": a["ocr_text"], "region": a["region_id"]}
                             for a in rec["assignments"] if a["ocr_text"].strip()],
                "unassigned": [{"id": u["ocr_id"], "bbox": u["bbox"], "text": u["ocr_text"], "reason": u["reason"],
                                "boundary": bool(u["candidate_region_ids"])}
                               for u in d["unassigned_texts"] if u["page_id"] == p["page_id"] and not u["empty_text"]],
                "gold": [], "page_eval": None, "issues": p["issues"]})
    data = {"settings": {}, "contract": docs[0]["contract"] if docs else None, "profile": docs[0]["parser_profile"] if docs else None,
            "checks": checks, "pages": pages, "has_eval": False}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    path = vdir / "index.html"
    path.write_text(_HTML.replace("__DATA__", blob), encoding="utf-8")
    return path


_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Layout Result</title><style>
:root{--ink:#1d232b;--mute:#5c6773;--line:#d8dde3;--ok:#1f5fbf;--q:#e67e00;--bad:#c62828;--ctx:#6a1b9a;--ocr:#78909c;--un:#c2185b;--gold:#6d4c41;--goldo:#00838f}
*{box-sizing:border-box}body{margin:0;font:13px/1.45 "Malgun Gothic",system-ui,sans-serif;color:var(--ink);background:#f6f7f9}
header{padding:8px 14px;background:#fff;border-bottom:1px solid var(--line)}header b{font-size:15px}header span{color:var(--mute);margin-left:10px}
#wrap{display:grid;grid-template-columns:200px minmax(0,1fr) 470px;height:calc(100vh - 40px)}
#nav,#side{overflow:auto;background:#fff}#nav{border-right:1px solid var(--line)}#side{border-left:1px solid var(--line)}
.p{padding:6px 10px;border-bottom:1px solid var(--line);cursor:pointer}.p.on{background:#e8f1fb}.p small{color:var(--mute);display:block}
.b{display:inline-block;padding:0 5px;border-radius:3px;color:#fff;font-size:11px;background:#8d6e00}.READY{background:#2e7d32}.CANDIDATE_RUN{background:#6a1b9a}
#strip{display:none;margin:10px 10px 0;padding:8px 10px;background:#f3e5f5;border:1px solid #6a1b9a;color:#4a148c;font-weight:bold}
#main{overflow:auto}#tools{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:6px 10px;z-index:3}
#tools label{margin-right:12px;white-space:nowrap}#legend{color:var(--mute);font-size:12px;margin-top:4px}
.sw{display:inline-block;width:18px;height:10px;border:2px solid;vertical-align:middle;margin:0 3px 0 8px}
#stage{position:relative;margin:10px}#stage img{width:100%;display:block}
#stage svg{position:absolute;inset:0;width:100%;height:100%}#banner{display:none;position:absolute;inset:0 0 auto 0;background:rgba(141,110,0,.92);color:#fff;padding:10px;font-weight:bold;z-index:2}
section{padding:8px 10px;border-bottom:1px solid var(--line)}h2{font-size:13px;margin:0 0 6px}pre{white-space:pre-wrap;background:#f4f5f7;padding:6px;margin:4px 0;font:12px/1.4 "Malgun Gothic",sans-serif}
pre.ocr{background:#fff;border:1px dashed var(--ocr)}
table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--line);padding:2px 4px;text-align:left;vertical-align:top}
.row{cursor:pointer}.row:hover{background:#f0f4f8}.row.on{background:#d9f2f2}.q{color:var(--q)}.bad{color:var(--bad)}.mute{color:var(--mute)}
.chip{display:inline-block;border:1px solid var(--line);border-radius:3px;padding:0 4px;margin:1px;font-size:11px}
.chip.q{border-color:var(--q);color:var(--q)}.chip.c{border-color:var(--ctx);color:var(--ctx)}
.tag{display:inline-block;font-size:11px;font-weight:bold;color:#fff;border-radius:3px;padding:0 5px;margin:6px 0 2px}
td.mg{background:#fff3e0}td.mg sup{color:var(--q);font-size:10px;margin-left:3px}td.emp{background:#fafafa}
.t-final{background:var(--ok)}.t-fail{background:var(--bad)}.t-q{background:var(--q)}.t-c{background:var(--ctx)}.t-ocr{background:var(--ocr)}.t-un{background:var(--un)}.t-gold{background:var(--gold)}
</style></head><body><header><b>최종 결과 대조</b><span id="hm"></span></header>
<div id="wrap"><div id="nav"></div><div id="main"><div id="tools"></div><div id="strip"></div><div id="stage"><div id="banner"></div><img id="img" alt=""><svg id="svg"></svg></div></div><div id="side"></div></div>
<script type="application/json" id="data">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent);let cur=0,sel=null,filt='all';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const L={regions:1,labels:1,context:1,ocr:0,gold:0};
const SRC={assigned:'배정',boundary_unassigned:'경계 걸침·미배정'};
const sid=id=>{const p=id.split('-');return /^t\d+$/.test(p[p.length-2])?p.slice(-2).join('-'):p[p.length-1]};
document.getElementById('hm').textContent=D.profile?`파일별 결과 ${D.contract} · 프로필 ${D.profile.name}: ${D.profile.read_mode} + ${D.profile.verify_mode} · 표 ${D.profile.table_mode} · 배정 ${D.profile.assignment_rule.name}(core ${D.profile.assignment_rule.core_shrink}) · 계약 검사 ${D.checks.passed?'통과':'실패'} · 상품 배정·라벨링: 미실행 · 내용 품질: 미검증`:`판독 ${D.settings.read_mode==='page-first'||D.settings.read_mode==='page-first-noocr'?D.settings.page_request+' + 영역 확인 '+D.settings.verify_request:'일반 '+D.settings.general_request} · 표 ${D.settings.table_read&&D.settings.table_read!=='legacy'?D.settings.table_read:D.settings.table_request} · 계약 검사 ${D.checks.passed?'통과':'실패'} · 내용 품질: 미검증(별도 평가)`;
const failed=r=>r.status==='read_failed'||r.status==='not_read'||(r.table&&r.table.status==='unknown');
function color(r){return failed(r)?'var(--bad)':(r.review.quality.length?'var(--q)':'var(--ok)')}
function nav(){let last=null,lastf=null;document.getElementById('nav').innerHTML=D.pages.map((p,i)=>{let head=p.sub!==last?`<div style="padding:4px 10px;background:#eef1f4;font-size:11px">${esc(p.sub)}</div>`:'';
if(p.order&&(p.file!==lastf||p.sub!==last))head+=`<div style="padding:4px 10px;background:#f7f9fb;font-size:12px;border-bottom:1px solid var(--line)"><b>${p.order}. ${esc(p.src)}</b><br><span class=mute>${esc(p.file)} · ${esc(p.ftype)} · ${p.fpages}쪽 · <a href="${p.result}" target="_blank">result.json</a></span></div>`;
last=p.sub;lastf=p.file;return head+`<div class="p ${i==cur?'on':''}" onclick="go(${i})"><b>${p.id}</b> <span class="b ${p.exec}">${p.exec}</span><small>${p.reading?`영역 ${p.reading.regions} · 판독 실패 ${p.reading.read_failed+p.reading.not_read} · 품질 검토 ${p.reading.needs_review_quality}`:esc(p.quality)}<br>${esc(p.file)} p${p.page_no} ${esc(p.src)}</small></div>`}).join('')}
function tools(){const ls=[['regions','영역'],['labels','ID·표 판정'],['context','맥락 검토(겹침·경계 걸침)'],['ocr','OCR 참고(배정·경계·미배정)']];if(D.has_eval)ls.push(['gold','평가용 정답 대조']);
document.getElementById('tools').innerHTML=ls.map(([k,n])=>`<label><input type=checkbox ${L[k]?'checked':''} onchange="L['${k}']=this.checked;draw();side()">${n}</label>`).join('')+
`<div id=legend>영역 테두리:<span class=sw style="border-color:var(--ok)"></span>VLM 최종 문구<span class=sw style="border-color:var(--q)"></span>VLM 최종 문구 + 품질 검토<span class=sw style="border-color:var(--bad)"></span>판독 실패(text=null)·표 판정 없음<span class=sw style="border-color:var(--ctx);border-style:dashed"></span>맥락 검토(겹친 범위 빗금)`+
(L.ocr?`<br>OCR(최종 아님):<span class=sw style="border-color:var(--ocr);border-width:1px"></span>배정 OCR<span class=sw style="border-color:var(--un);border-style:dashed"></span>경계 걸친 미배정<span class=sw style="border-color:var(--un);background:rgba(194,24,91,.15)"></span>영역 밖 미배정`:'')+
(L.gold?`<br>정답(평가용):<span class=sw style="border-color:var(--gold);background:rgba(109,76,65,.18)"></span>어디에도 없음<span class=sw style="border-color:var(--goldo)"></span>소속 영역 밖에만 있음(다른 영역·미배정 OCR 등)`:'')+`</div>`}
function rect(b,a,t){return `<rect x="${b[0]}" y="${b[1]}" width="${b[2]-b[0]}" height="${b[3]-b[1]}" ${a}><title>${esc(t)}</title></rect>`}
function draw(){tools();const p=D.pages[cur],sw=Math.max(1.5,p.w/700),fs=Math.max(11,p.w/95);let s=`<defs><pattern id="hatch" width="${sw*6}" height="${sw*6}" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="${sw*2}" height="${sw*6}" fill="rgba(106,27,154,.35)"/></pattern></defs>`;const svg=document.getElementById('svg');svg.setAttribute('viewBox',`0 0 ${p.w} ${p.h}`);svg.setAttribute('preserveAspectRatio','none');
if(L.ocr){for(const a of p.assigned)s+=rect(a.bbox,`fill="none" stroke="var(--ocr)" stroke-width="${sw*.7}"`,`배정 OCR(최종 아님) → ${a.region}: ${a.text}`);
for(const u of p.unassigned)s+=rect(u.bbox,u.boundary?`fill="none" stroke="var(--un)" stroke-width="${sw}" stroke-dasharray="${sw*3} ${sw*2}"`:`fill="rgba(194,24,91,.15)" stroke="var(--un)" stroke-width="${sw}"`,`미배정 OCR(최종 아님) ${u.reason}: ${u.text}`)}
if(L.gold)for(const g of p.gold){if(!g.bbox||g.status.startsWith('home'))continue;const m=g.status==='missing';s+=rect(g.bbox,`fill="${m?'rgba(109,76,65,.18)':'none'}" stroke="${m?'var(--gold)':'var(--goldo)'}" stroke-width="${sw*1.3}"`,`정답 ${g.id} ${g.status}: ${g.text}`)}
if(L.context)for(const o of p.overlaps)s+=rect(o.box,`fill="url(#hatch)" stroke="var(--ctx)" stroke-width="${sw}"`,`영역 겹침 ${o.a} ↔ ${o.b} (IoU ${o.iou})`);
if(L.regions)for(const r of p.regions){const c=color(r),on=sel===r.id;s+=rect(r.bbox,`fill="${on?'rgba(0,163,163,.12)':'none'}" stroke="${on?'#00a3a3':c}" stroke-width="${on?sw*3:sw*1.4}" style="cursor:pointer" onclick="pick('${r.id}')"`,`${r.id} ${r.status}`);
if(L.context&&r.review.context.length&&!on)s+=rect([r.bbox[0]+sw*2,r.bbox[1]+sw*2,r.bbox[2]-sw*2,r.bbox[3]-sw*2],`fill="none" stroke="var(--ctx)" stroke-width="${sw}" stroke-dasharray="${sw*4} ${sw*3}" pointer-events="none"`,r.review.context.join(', '));
if(L.labels){const t=r.table?(r.table.status==='table'?' 표✓':r.table.status==='not_table'?' 표✗':' 표?'):'';s+=`<text x="${r.bbox[0]+2}" y="${r.bbox[1]+fs}" font-size="${fs}" fill="${c}" stroke="#fff" stroke-width="${fs/5}" paint-order="stroke" font-weight="bold">${sid(r.id)}${t}</text>`}}
if(p.plan&&p.plan.cuts)for(const y of p.plan.cuts.slice(1,-1))s+=`<line x1="0" x2="${p.w}" y1="${y}" y2="${y}" stroke="#ef6c00" stroke-width="${sw*3}" stroke-dasharray="${sw*8} ${sw*5}"><title>후보 경계 y=${y}(미검증)</title></line>`;
for(const t of p.tiles||[])s+=`<text x="${p.w-4}" y="${t.crop[1]+fs*1.2}" text-anchor="end" font-size="${fs}" fill="#ef6c00" stroke="#fff" stroke-width="${fs/5}" paint-order="stroke" font-weight="bold">${t.id.split('-').pop()} y ${t.crop[1]}–${t.crop[3]} (비율 ${t.aspect})</text>`;
svg.innerHTML=s;const bn=document.getElementById('banner'),st=document.getElementById('strip');
if(p.exec==='CANDIDATE_RUN'){bn.style.display='none';st.style.display='block';st.textContent=`후보 경계 실험 실행 · 경계 미검증 · 확정 결과 아님 — ${p.basis}`}
else if(p.exec!=='READY'){st.style.display='none';bn.style.display='block';bn.textContent=`${p.exec}: ${p.basis}`}else{bn.style.display='none';st.style.display='none'}}
function pick(id){sel=id;draw();side();const el=document.getElementById('row-'+id);if(el)el.scrollIntoView({block:'center'})}
function cellsTable(t){let h='<table>';for(let i=0;i<t.n_rows;i++){h+='<tr>';for(const c of t.cells.filter(c=>c.row===i).sort((a,b)=>a.col-b.col)){const mg=c.rowspan>1||c.colspan>1;
h+=`<td rowspan="${c.rowspan}" colspan="${c.colspan}" class="${mg?'mg':(c.text?'':'emp')}">${esc(c.text)||'<span class=mute>(빈 칸)</span>'}${mg?`<sup>${c.rowspan>1?'↕'+c.rowspan:''}${c.colspan>1?'↔'+c.colspan:''}</sup>`:''}</td>`}h+='</tr>'}return h+'</table>'}
function detail(r){let s='';
if(r.text===null)s+=`<div class="tag t-fail">판독 실패 · 최종 문구 없음 (${esc(r.status)})</div>`;else s+=`<div class="tag t-final">VLM 최종 문구</div><pre>${esc(r.text)||'(빈 문자열: 글자 없음으로 판독)'}</pre>`;
if(r.table){s+=`<div class="tag t-final">VLM 표 확인: ${r.table.status==='table'?'표':r.table.status==='not_table'?'표 아님(영역·문구는 보존)':'판정 없음'}${r.table.structure?' · '+esc(r.table.structure):''}</div>`;
if(r.table.cells){s+=`<div class=mute>논리 격자 ${r.table.n_rows}행 × ${r.table.n_cols}열 · 주황 칸 = 병합(↕행 수 ↔열 수, 문구는 시작 칸에 한 번)</div>`+cellsTable(r.table)}
else if(r.table.rows)s+='<table>'+r.table.rows.map(row=>'<tr>'+row.map(c=>`<td>${esc(c)}</td>`).join('')+'</tr>').join('')+'</table>';
if(r.table_notes)s+=`<div class=mute>표 판독 notes: ${esc(r.table_notes)}</div>`;
if(r.table_cells_errors)s+=`<div class=bad>표 구조 판독 계약 실패(이전 묶음 유지): ${esc(r.table_cells_errors.join('; '))}</div>`;
if(r.table.cells&&r.page_rows)s+=`<div class="tag t-ocr">VLM 1차 페이지 판독의 표 행 · 최종 아님(구조 판독 전)</div><table class=ocr>${r.page_rows.map(row=>'<tr>'+row.map(c=>`<td>${esc(c)}</td>`).join('')+'</tr>').join('')}</table>`}
s+=`<div class="tag t-q">품질 검토</div> ${r.review.quality.map(x=>`<span class="chip q">${x}</span>`).join('')||'<span class=mute>근거 없음</span>'}`;
s+=`<br><div class="tag t-c">맥락 검토</div> ${r.review.context.map(x=>`<span class="chip c">${x}</span>`).join('')||'<span class=mute>근거 없음</span>'}`;
if(r.review.measures&&Object.keys(r.review.measures).length)s+=`<div class=mute>측정값 ${esc(JSON.stringify(r.review.measures))}</div>`;
s+=`<div class=mute>판독 경로: ${esc(r.read_path||'')}${r.page_view?' · 판독 단위 '+esc(r.page_view):''}${r.verify_reasons?' · 영역 확인 사유 '+esc(r.verify_reasons.join(', ')):''}</div>`;
if(r.patch_applied&&r.patch_applied.length)s+=`<div class=mute>영역 확인 반영: ${esc(JSON.stringify(r.patch_applied))}</div>`;
if(r.patch_not_applied&&r.patch_not_applied.length)s+=`<div class=q>영역 확인 미반영(기준 문자열 불일치): ${esc(JSON.stringify(r.patch_not_applied))}</div>`;
if(r.reread_source)s+=`<div class=mute>영역 재판독 선택: ${r.reread_source==='reread'?'재판독 후보':'1차 판독 유지'} · 변경 ${esc((r.reread_changes||[]).join(', '))} · ${esc(r.reread_notes||'')}</div>`;
if(r.reread_source==='first_pass'&&r.reread_text)s+=`<div class="tag t-ocr">VLM 재판독 후보 · 선택되지 않음(최종 아님)</div><pre class=ocr>${esc(r.reread_text)}</pre>`;
if(r.page_text!==undefined&&r.page_text!==null&&r.page_text!==r.text)s+=`<div class="tag t-ocr">VLM 1차 페이지 판독 · 영역 확인 전(최종 아님)</div><pre class=ocr>${esc(r.page_text)}</pre>`;
if(r.candidate)s+=`<div class="tag t-c">미검증 후보 경계 타일의 결과</div>`;
if(r.pa){const st=x=>({not_run:'미실행',completed:'완료',partial:'일부',failed:'실패'}[x]||x);s+=`<br><div class="tag t-ocr">상품 배정</div> ${st(r.pa.status)}${r.pa.status==='not_run'?' <span class=mute>(빈 값은 \'상품 없음\' 판정이 아님)</span>':` · ${esc(r.pa.scope)} · ${esc(r.pa.product_ids.join(', '))}`}`;
s+=`<br><div class="tag t-ocr">라벨링</div> ${st(r.lb.status)}${r.lb.status==='not_run'?' <span class=mute>(빈 값은 \'라벨 없음\' 판정이 아님)</span>':' · '+esc(JSON.stringify(r.lb.items))}`}
if(L.ocr||r.text===null){s+=`<div class="tag t-ocr">OCR 참고 원문 · 최종 문구 아님 (원래 OCR 순서)</div><pre class=ocr>${r.ocr.map(c=>esc(c.text)+(c.src!=='assigned'?`  [${SRC[c.src]}]`:'')).join('\n')||'(없음)'}</pre>`}
if(L.gold&&r.eval)s+=`<div class="tag t-gold">평가용 정답 대조</div><div class=mute>정확 ${r.eval.lines_exact} 근사 ${r.eval.lines_approx_only} / 기대 ${r.eval.expected_lines} · 누락 ${esc(r.eval.missing_lines)} · 근거 없는 글자 ${r.eval.unsupported_chars} 추가 구간 ${esc(r.eval.added_segments)} · 숫자 누락 ${esc(r.eval.missing_digits)} 추가 ${esc(r.eval.extra_digits)}</div>`;
s+=`<div class=mute>raw label ${esc(r.label)} · 호출 ${esc((r.calls||[]).join(', '))}</div>`;return s}
function side(){const p=D.pages[cur];const rs=p.regions.filter(r=>filt==='all'||(filt==='q'&&r.review.quality.length)||(filt==='c'&&r.review.context.length)||(filt==='fail'&&failed(r))||(filt==='table'&&r.table));
let s=`<section><h2>${p.id} · ${esc(p.src)}</h2><div class=mute>${esc(p.basis)}</div><div>실행 ${p.exec} · 내용 품질 ${p.quality}</div>`;
if(p.reading)s+=`<div class=mute>${esc(JSON.stringify(p.reading))}</div>`;if(L.gold&&p.page_eval)s+=`<div><span class="tag t-gold">평가용</span> 정답 줄 ${p.page_eval.required_lines}: <span class=mute>${esc(JSON.stringify(p.page_eval.counts))}</span></div>`;
s+=`<div style="margin-top:6px">${[['all','전체'],['q','품질 검토'],['c','맥락 검토'],['fail','판독 실패'],['table','표 후보']].map(([k,n])=>`<label><input type=radio name=f ${filt==k?'checked':''} onchange="filt='${k}';side()">${n}</label>`).join(' ')}</div></section><section><h2>영역별 최종 결과 ${rs.length}</h2>`;
s+=rs.map(r=>`<div id="row-${r.id}" class="row ${sel===r.id?'on':''}" onclick="pick('${r.id}')"><b style="color:${color(r)}">${sid(r.id)}</b> ${r.table?'['+r.table.status+'] ':''}${r.review.context.length?'<span class="chip c">맥락</span>':''}<span class=mute>${r.text===null?'(판독 실패)':esc((r.text||'').slice(0,60))}</span></div>`+(sel===r.id?detail(r):'')).join('');
s+='</section>';
s+=`<section><h2><span class="tag t-un">미배정 OCR · 최종 문구 아님</span> ${p.unassigned.length}건</h2>${L.ocr?p.unassigned.map(u=>`<div class=mute>${esc(u.reason)}: ${esc(u.text)}</div>`).join(''):'<div class=mute>OCR 참고 레이어를 켜면 목록과 위치를 표시</div>'}</section>`;
if(L.gold){const miss=p.gold.filter(g=>!g.status.startsWith('home'));if(miss.length)s+=`<section><h2><span class="tag t-gold">평가용</span> 정답 줄 중 소속 영역 최종 문구에 없는 줄 ${miss.length}</h2>${miss.map(g=>`<div><span class="${g.status==='missing'?'bad':'q'}">${g.status}</span> ${esc(g.text)}</div>`).join('')}</section>`}
s+=`<section><h2>이슈</h2>${p.issues.map(i=>`<div class=mute>${i.code}: ${esc(i.message)}</div>`).join('')}</section>`;document.getElementById('side').innerHTML=s}
function go(i){cur=i;sel=null;document.getElementById('img').src=D.pages[i].img;nav();draw();side();history.replaceState(null,'','#'+D.pages[i].id)}
const h=decodeURIComponent(location.hash.slice(1)).split('/');const i0=D.pages.findIndex(p=>p.id===h[0]);go(i0<0?0:i0);if(h[1])pick(h[1]);
</script></body></html>
"""
