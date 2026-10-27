"""원본 페이지 대조 화면(정적 HTML). 페이지 이미지는 원본 파일을 상대 경로로 참조하고 SVG 로 겹쳐 그린다.

좌표는 SVG viewBox = 페이지 픽셀이므로 bbox 를 변환 없이 그대로 그린다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .jsonio import load


def _page_data(rec: dict, calls: list[dict], viewer_dir: Path) -> dict:
    page = rec["page"]
    gold = rec.get("gold") or {}
    return {
        "id": page["page_id"], "w": page["width"], "h": page["height"],
        "img": os.path.relpath(page["image_path"], viewer_dir).replace("\\", "/"),
        "kind": page["source"]["kind"], "source": page["source"].get("source_name"),
        "status": rec["status"], "route": rec["route"]["route"], "shape": rec["route"]["shape"],
        "aspect": page["geometry"]["aspect"], "basis": rec["route"]["basis"],
        "views": rec["route"]["views"],
        "calls": [{k: c.get(k) for k in ("call_id", "view_id", "view_kind", "input_crop", "sent_size", "sent_bytes",
                                          "sent_sha256", "seconds", "ok", "error", "response_size",
                                          "response_size_matches", "text_det_params", "text_det_resize", "counts",
                                          "started_at")} | {"page_sha": page["image_sha256"]}
                  for c in calls if c["page_id"] == page["page_id"]],
        "regions": [{"id": r["region_id"], "idx": r["source_index"], "bbox": r["bbox"], "table": r["table_candidate"],
                     "label": r["raw_label"], "score": round(r["raw_score"], 4), "assigned": r["assigned_ocr_ids"],
                     "crossing": r["crossing_ocr_ids"], "ambiguous": r["ambiguous_ocr_ids"], "draft": r["ocr_draft"]}
                    for r in rec["regions"]],
        "assigned": [{"id": a["ocr_id"], "region": a["region_id"], "bbox": a["ocr_bbox"], "text": a["ocr_text"],
                      "exact": a["exact_inside"], "protrusion": a["protrusion_px"], "score": round(a["score"], 4),
                      "empty": a["empty_text"]} for a in rec["assignments"]],
        "unassigned": [{"id": u["ocr_id"], "bbox": u["ocr_bbox"], "text": u["ocr_text"], "reason": u["reason"],
                        "reason_text": u["reason_text"], "score": round(u["score"], 4), "empty": u["empty_text"],
                        "candidates": [{k: c[k] for k in ("region_id", "inside_ratio", "protrusion_px", "core_inside")}
                                       for c in u["candidates"]]} for u in rec["unassigned_texts"]],
        "overlaps": rec["diagnostics"]["region_overlaps"],
        "tile_edges": rec["diagnostics"]["tile_edge_regions"],
        "no_text_regions": rec["diagnostics"]["regions_without_text"],
        "blocks": [{"id": b["block_id"], "label": b["block_label"], "bbox": b["bbox"]} for b in rec["server_blocks"]],
        "block_cmp": rec["diagnostics"]["server_block_comparison"],
        "gold": {"evaluated": gold.get("evaluated"), "required": gold.get("required_lines"),
                 "counts": gold.get("counts"), "reason": gold.get("reason"),
                 "uncovered": [{"id": x["gold_id"], "bbox": x["bbox"], "text": x["text"], "status": x["status"],
                                "geometry": x.get("geometry"), "ratio": x.get("best_inside_ratio"),
                                "candidates": x["candidates"]} for x in gold.get("lines", [])
                               if not x["optional"] and (x["status"] != "covered" or x.get("geometry") != "single")],
                 "geometry": (gold.get("geometry") or {}).get("counts"),
                 "policy": (gold.get("assignment_policy") or {}).get("counts"),
                 "tables": (gold.get("tables") or {}).get("gold_tables", []),
                 "unlisted_candidates": (gold.get("tables") or {}).get("candidates_not_in_gold_tables", [])},
        "issues": rec["issues"], "partition": rec["partition_checks"], "quality": rec.get("quality"),
        "plan": {k: (rec["route"].get("boundary_plan") or {}).get(k) for k in
                 ("status", "plan_version", "axis", "cuts", "boundaries", "policy", "failure")}
        if rec["route"].get("boundary_plan") else None,
        "vlm": rec["vlm_plan"]["checks"],
    }


def build_viewer(run_dir: Path) -> Path:
    viewer_dir = run_dir / "viewer"
    viewer_dir.mkdir(parents=True, exist_ok=True)
    calls = load(run_dir / "calls.json")
    report = load(run_dir / "report.json")
    pages = [p["page_id"] for p in load(run_dir / "pages.json")]
    data = {"meta": {k: report["meta"].get(k) for k in ("created_at", "rebuilt_at", "mode", "layout_options",
                                                         "routing_policy", "assignment_rule", "pdf_dpi", "ocr_options")},
            "totals": report["totals"], "regress": report["a4_square_whole_once"],
            "pages": [_page_data(load(run_dir / "records" / f"{pid}.json"), calls, viewer_dir) for pid in pages]}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = viewer_dir / "index.html"
    out.write_text(_HTML.replace("__DATA__", blob), encoding="utf-8")
    return out


_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Layout Stage1 대조</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1d232b;--mute:#5c6773;--line:#d8dde3;--region:#1f5fbf;--table:#c62828;
--assigned:#2e7d32;--outside:#c2185b;--partial:#ef6c00;--ambig:#6a1b9a;--gold:#6d4c41;--block:#7a7a7a;--sel:#00a3a3}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 "Malgun Gothic",system-ui,sans-serif}
header{padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:16px;flex-wrap:wrap;align-items:baseline}
header h1{font-size:15px;margin:0}header .m{color:var(--mute)}
#wrap{display:grid;grid-template-columns:210px minmax(0,1fr) 430px;height:calc(100vh - 42px)}
#nav,#side{overflow:auto;background:var(--panel);border-right:1px solid var(--line)}#side{border-left:1px solid var(--line);border-right:0}
#nav .p{padding:6px 10px;border-bottom:1px solid var(--line);cursor:pointer}#nav .p.on{background:#e8f1fb}
#nav .p b{display:block}#nav .p small{color:var(--mute)}
.badge{display:inline-block;padding:0 5px;border-radius:3px;font-size:11px;color:#fff;background:#555}
.READY{background:#2e7d32}.BOUNDARY_PENDING,.BOUNDARY_CANDIDATE_UNVALIDATED,.NO_SAFE_BOUNDARY,.OVERSIZED_REGION{background:#8d6e00}.OCR_FAILED,.BOUNDARY_PLAN_INVALID{background:#c62828}
#main{overflow:auto;position:relative}#tools{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:6px 10px;display:flex;flex-wrap:wrap;gap:10px}
#tools label{white-space:nowrap}.sw{display:inline-block;width:10px;height:10px;margin-right:3px;vertical-align:-1px;border:2px solid}
#stage{position:relative;margin:10px}#stage img{display:block;width:100%;height:auto}#stage svg{position:absolute;inset:0;width:100%;height:100%}
#banner{display:none;position:absolute;left:0;right:0;top:0;padding:10px;background:rgba(141,110,0,.92);color:#fff;font-weight:bold;z-index:3}
#side section{padding:8px 10px;border-bottom:1px solid var(--line)}#side h2{font-size:13px;margin:0 0 6px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:2px 4px;text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:normal}.row{cursor:pointer}.row:hover{background:#f0f4f8}.row.on{background:#d9f2f2}
.tabs button{border:1px solid var(--line);background:#fff;padding:3px 8px;cursor:pointer}.tabs button.on{background:#1d232b;color:#fff}
.mono{font-family:Consolas,monospace;font-size:12px}.mute{color:var(--mute)}pre{white-space:pre-wrap;margin:4px 0;background:#f4f5f7;padding:6px}
.r-outside_layout{color:var(--outside)}.r-partial_overlap{color:var(--partial)}.r-ambiguous_multiple_regions,.r-ambiguous_overlapping_regions{color:var(--ambig)}
</style></head><body>
<header><h1>Stage1 대조 화면</h1><span class="m" id="hmeta"></span></header>
<div id="wrap"><div id="nav"></div>
<div id="main"><div id="tools"></div><div id="stage"><div id="banner"></div><img id="img" alt=""><svg id="svg"></svg></div></div>
<div id="side"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const LAYERS=[['views','입력 범위','#000'],['regions','레이아웃 영역','var(--region)'],['tables','표 후보','var(--table)'],
['labels','영역 ID','#444'],['assigned','배정 OCR','var(--assigned)'],['outside','미배정: 밖','var(--outside)'],
['partial','미배정: 부분','var(--partial)'],['ambig','미배정: 모호','var(--ambig)'],['overlaps','원시 중첩','#e0a800'],
['empty','빈 문자열 OCR','#9e9e9e'],['gold','정답 줄 미포함','var(--gold)'],['blocks','서버 parsing_res_list(비교)','var(--block)']];
LAYERS.splice(6,0,['margin','미배정: 여백만 걸침','#00897b']);
const on={views:1,regions:1,tables:1,labels:1,assigned:1,outside:1,margin:1,partial:1,ambig:1,empty:1,overlaps:1,gold:1,blocks:0};
let cur=0,sel=null,tab='calls';const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=D.totals;document.getElementById('hmeta').textContent=`레이아웃 옵션=${D.meta.layout_options&&Object.keys(D.meta.layout_options).length?JSON.stringify(D.meta.layout_options):'서버 기본'} · 분기 long≥${D.meta.routing_policy.long_aspect_min}[${D.meta.routing_policy.status}] · 배정 ${D.meta.assignment_rule.name}(core ${D.meta.assignment_rule.core_shrink}) · OCR 호출 ${t.ocr_calls} · 영역 ${t.regions} · 표 후보 ${t.table_candidates} · 최종 문구: 미판독`;
function nav(){document.getElementById('nav').innerHTML=D.pages.map((p,i)=>{const un=p.unassigned.filter(u=>!u.empty).length;
return `<div class="p ${i==cur?'on':''}" onclick="go(${i})"><b>${p.id} <span class="badge ${p.status}">${p.status}</span></b><small>${p.kind} ${p.w}×${p.h} · ${p.aspect} · ${p.shape}/${p.route}<br>OCR ${p.calls.length}회 · 영역 ${p.regions.length} · 표 ${p.regions.filter(r=>r.table).length} · 미배정 ${un}</small></div>`}).join('')}
function tools(){document.getElementById('tools').innerHTML=LAYERS.map(([k,n,c])=>`<label><input type="checkbox" ${on[k]?'checked':''} onchange="on['${k}']=this.checked;draw()"><span class="sw" style="border-color:${c}"></span>${n}</label>`).join('')}
function rect(b,a,t){return `<rect x="${b[0]}" y="${b[1]}" width="${b[2]-b[0]}" height="${b[3]-b[1]}" ${a}${t?t+'>':'/>'}`}
function draw(){const p=D.pages[cur],sw=Math.max(1.5,p.w/700),fs=Math.max(11,p.w/95);let s='';
const svg=document.getElementById('svg');svg.setAttribute('viewBox',`0 0 ${p.w} ${p.h}`);svg.setAttribute('preserveAspectRatio','none');
const isSel=id=>sel&&sel.id===id;const rel=sel&&sel.kind==='region'?D.pages[cur].regions.find(r=>r.id===sel.id):null;
if(on.overlaps)for(const o of p.overlaps)s+=rect(o.box,'fill="rgba(255,193,7,.35)" stroke="none"',`><title>${o.a} ∩ ${o.b} ${o.relation} IoU ${o.iou}</title></rect`);
if(on.blocks)for(const b of p.blocks)s+=rect(b.bbox,`fill="none" stroke="var(--block)" stroke-width="${sw}" stroke-dasharray="${sw*4} ${sw*3}"`,`><title>parsing_res_list #${b.id} ${esc(b.label)} (비교용, Region 아님)</title></rect`);
for(const r of p.regions){if(r.table?!on.tables:!on.regions)continue;const c=r.table?'var(--table)':'var(--region)';
s+=rect(r.bbox,`fill="${isSel(r.id)?'rgba(0,163,163,.12)':'none'}" stroke="${isSel(r.id)?'var(--sel)':c}" stroke-width="${isSel(r.id)?sw*2.5:sw*1.4}" style="cursor:pointer" onclick="pick('region','${r.id}')"`,`><title>${r.id} (layout_det_res #${r.idx}) ${r.table?'표 후보':''} score ${r.score}</title></rect`);
if(on.labels)s+=`<text x="${r.bbox[0]+2}" y="${r.bbox[1]+fs}" font-size="${fs}" fill="${c}" stroke="#fff" stroke-width="${fs/5}" paint-order="stroke" font-weight="bold">${r.table?'T':''}L${String(r.idx).padStart(3,'0')}</text>`}
if(on.assigned)for(const a of p.assigned){const hl=rel&&rel.assigned.includes(a.id)||isSel(a.id);
s+=rect(a.bbox,`fill="${hl?'rgba(46,125,50,.25)':'none'}" stroke="var(--assigned)" stroke-width="${sw*(a.exact?0.7:1.2)}" ${a.exact?'':'stroke-dasharray="'+sw*2+' '+sw*1.5+'"'}`,`><title>${a.id} → ${a.region}${a.exact?'':' (허용 오차, 돌출 '+a.protrusion+'px)'}: ${esc(a.text)}</title></rect`)}
for(const u of p.unassigned){if(u.empty){if(on.empty)s+=rect(u.bbox,`fill="none" stroke="#9e9e9e" stroke-width="${sw}" stroke-dasharray="${sw} ${sw*2}"`,`><title>${u.id} 빈 문자열 OCR(${u.reason})</title></rect`);continue}
const k=u.reason==='outside_layout'?'outside':u.reason==='margin_overlap_only'?'margin':u.reason==='partial_overlap'?'partial':'ambig';if(!on[k])continue;
const c=k==='outside'?'var(--outside)':k==='margin'?'#00897b':k==='partial'?'var(--partial)':'var(--ambig)';const hl=isSel(u.id)||rel&&(rel.crossing.includes(u.id)||rel.ambiguous.includes(u.id));
s+=rect(u.bbox,`fill="${hl?'rgba(194,24,91,.25)':'rgba(255,255,255,.01)'}" stroke="${c}" stroke-width="${sw*1.6}" style="cursor:pointer" onclick="pick('ocr','${u.id}')"`,`><title>${u.id} 미배정 ${u.reason}: ${esc(u.text)}</title></rect`)}
if(on.gold)for(const g of p.gold.uncovered||[])s+=rect(g.bbox,`fill="none" stroke="var(--gold)" stroke-width="${sw*1.2}" stroke-dasharray="${g.geometry==='none'?'':sw+' '+sw}"`,`><title>정답 ${g.id} 기하 ${g.geometry}(${g.ratio}) / 배정 ${g.status}: ${esc(g.text)}</title></rect`);
if(on.views)for(const v of p.views)s+=rect(v.crop,`fill="none" stroke="#000" stroke-width="${sw*2}" stroke-dasharray="${sw*6} ${sw*4}"`,`><title>${v.view_id} ${v.kind} 입력 ${v.crop}</title></rect`);
if(p.plan&&p.plan.cuts){const sem={};for(const b of p.plan.boundaries||[])sem[b.position]=b.semantic_check||{};
for(const y of p.plan.cuts.slice(1,-1)){const r=(sem[y]||{}).result,c=r==='safe'?'#2e7d32':r==='unsafe'?'#c62828':'#ef6c00';
s+=`<line x1="0" x2="${p.w}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="${sw*3}" stroke-dasharray="${sw*8} ${sw*5}"><title>${p.plan.status} cut y=${y}: ${esc(r)} ${esc((sem[y]||{}).note)}</title></line>`;
s+=`<text x="4" y="${y-6}" font-size="${fs*1.3}" fill="${c}" stroke="#fff" stroke-width="${fs/4}" paint-order="stroke" font-weight="bold">${p.plan.status} y=${y} (${r||'근거 없음'})</text>`}}
svg.innerHTML=s;const bn=document.getElementById('banner');
if(p.status!=='READY'){bn.style.display='block';bn.textContent=`${p.status}: ${p.basis}`}else bn.style.display='none'}
function pick(kind,id){sel={kind,id};draw();if(kind==='region')tab='regions';else tab='unassigned';side();const el=document.getElementById('row-'+id);if(el)el.scrollIntoView({block:'center'})}
function plan(p){if(!p.plan)return '';const q=p.plan;return `<h2 style="margin-top:8px">경계 문서 ${esc(q.plan_version)} [${esc(q.status)}]</h2>
<div class="mono">정책 ${esc(JSON.stringify(q.policy))}</div><table><tr><th>cut</th><th>의미 검증</th><th>근거</th></tr>`+(q.boundaries||[]).map(b=>`<tr><td>${b.position}</td><td>${esc((b.semantic_check||{}).method)} → <b>${esc((b.semantic_check||{}).result)}</b><br>${esc((b.semantic_check||{}).note)}</td><td class="mono">${esc(JSON.stringify(b.evidence))}</td></tr>`).join('')+'</table>'+(q.failure?`<p>${esc(JSON.stringify(q.failure))}</p>`:'')+'<p class="mute">candidate 경계는 실행하지 않는다. validated 는 사람 검토 방식과 경계별 근거·해시·정책 한도가 모두 있어야 한다.</p>'}
function calls(p){if(!p.calls.length)return `<p>OCR 호출 없음. ${esc(p.basis)}</p>`+plan(p);
return p.calls.map(c=>`<table class="mono"><tr><th>call</th><td>${c.call_id} (${c.view_kind}) ${c.ok?'성공':'<b style=color:red>실패</b> '+esc(c.error)}</td></tr>
<tr><th>입력 범위</th><td>[${c.input_crop}] ${c.view_kind==='whole'&&c.input_crop.join()==[0,0,p.w,p.h].join()?'= 페이지 전체':''}</td></tr>
<tr><th>보낸 이미지</th><td>${c.sent_size.join('×')} · ${c.sent_bytes.toLocaleString()} bytes</td></tr>
<tr><th>sha256</th><td>${c.sent_sha256.slice(0,16)}… ${c.sent_sha256===c.page_sha?'= 페이지 PNG':'(타일 crop)'}</td></tr>
<tr><th>응답 크기</th><td>${c.response_size} 일치=${c.response_size_matches}</td></tr><tr><th>시간</th><td>${c.seconds}s · ${c.started_at}</td></tr>
<tr><th>원시 개수</th><td>layout_det ${c.counts.layout_det_boxes} · OCR ${c.counts.ocr_items} · parsing_res ${c.counts.parsing_res_blocks}</td></tr>
<tr><th>text_det</th><td>${esc(JSON.stringify(c.text_det_params))}<br>배율 추정 ${c.text_det_resize?c.text_det_resize.ratio:'-'}</td></tr></table>`).join('<hr>')}
function regions(p){return `<p class="mute">출처 prunedResult.layout_det_res.boxes. 분류명은 trace 전용. 클릭하면 배정 OCR(초록)·경계에 걸친 OCR(분홍)을 강조.</p><table><tr><th>ID</th><th>표</th><th>배정</th><th>걸침/모호</th><th>raw label</th></tr>`+
p.regions.map(r=>`<tr id="row-${r.id}" class="row ${sel&&sel.id===r.id?'on':''}" onclick="pick('region','${r.id}')"><td class="mono">${r.id.split('-').pop()}</td><td>${r.table?'<b style=color:var(--table)>후보</b>':''}</td><td>${r.assigned.length}</td><td>${r.crossing.length}/${r.ambiguous.length}</td><td class="mute">${esc(r.label)} ${r.score}</td></tr>`+
(sel&&sel.id===r.id?`<tr><td colspan=5><div class="mono">bbox [${r.bbox.map(v=>v.toFixed(1))}]</div><div class="mute">OCR 초안(최종 문구 아님):</div><pre>${esc(r.draft)||'(배정 OCR 없음)'}</pre>${r.crossing.length?'<div>경계에 걸침: '+r.crossing.join(', ')+'</div>':''}${r.ambiguous.length?'<div>모호: '+r.ambiguous.join(', ')+'</div>':''}${ovl(p,r.id)}</td></tr>`:'')).join('')+'</table>'}
function ovl(p,id){const o=p.overlaps.filter(o=>o.a===id||o.b===id);return o.length?'<div>원시 중첩: '+o.map(o=>`${(o.a===id?o.b:o.a).split('-').pop()} ${o.relation} IoU ${o.iou}`).join('; ')+'</div>':''}
function unassigned(p){const rows=p.unassigned.filter(u=>!u.empty);const emp=p.unassigned.length-rows.length;
return `<p class="mute">overall_ocr_res 원문(최종 영역 아님, 검토 대상). 빈 문자열 OCR ${emp}개는 목록에서 생략(JSON 에는 보존).</p><table><tr><th>ID</th><th>사유</th><th>OCR 원문</th><th>후보 영역(포함비)</th></tr>`+
rows.map(u=>`<tr id="row-${u.id}" class="row ${sel&&sel.id===u.id?'on':''}" onclick="pick('ocr','${u.id}')"><td class="mono">${u.id.split('-').pop()}</td><td class="r-${u.reason}">${u.reason}</td><td>${esc(u.text)}</td><td class="mono">${u.candidates.map(c=>c.region_id.split('-').pop()+' '+c.inside_ratio+(c.core_inside?'*':'')).join('<br>')}</td></tr>`).join('')+'</table><p class="mute">* = 중심부 포함</p>'}
function issues(p){const g=p.gold;let s='<table><tr><th>코드</th><th>내용</th></tr>'+p.issues.map(i=>`<tr><td class="mono">${i.code}</td><td>${esc(i.message)}</td></tr>`).join('')+'</table>';
s+=`<h2 style="margin-top:10px">분할 검증</h2><div class="mono">${esc(JSON.stringify({ocr_items:p.partition.ocr_items,assigned:p.partition.assigned,unassigned:p.partition.unassigned,missing:p.partition.missing.length,duplicated:p.partition.duplicated.length,passed:p.partition.passed}))}</div>`;
s+=`<h2 style="margin-top:10px">layout_det_res vs parsing_res_list</h2><div>${p.block_cmp.layout_det_boxes} vs ${p.block_cmp.parsing_res_blocks}. 대응 블록 없는 영역 ${p.block_cmp.regions_without_matching_block.length}, 대응 영역 없는 블록 ${p.block_cmp.blocks_without_matching_region.length} (IoU≥0.5). Region 은 layout_det_res 만 사용.</div>`;
s+=`<h2 style="margin-top:10px">VLM 계획(미실행)</h2><div>read_text ${p.vlm.read_text} · table_check ${p.vlm.table_check} (표 후보만)</div>`;
if(g.evaluated){s+=`<h2 style="margin-top:10px">정답 줄: 기하 단일 포함 아님 또는 배정 실패 ${g.uncovered.length}/${g.required}</h2><table><tr><th>ID</th><th>기하(포함비)</th><th>배정</th><th>정답</th></tr>`+g.uncovered.map(x=>`<tr><td class="mono">${x.id.split('-').pop()}</td><td>${x.geometry}(${x.ratio})</td><td>${x.status}</td><td>${esc(x.text)}</td></tr>`).join('')+'</table>';
if(g.tables.length)s+='<h2 style="margin-top:10px">정답 표</h2>'+g.tables.map(x=>`<div>${x.gold_table}: ${x.status} (${(x.best_fraction*100).toFixed(0)}% in ${x.best_candidate||'-'})</div>`).join('');
s+=`<div class="mute">정답 표 목록에 없는 표 후보: ${g.unlisted_candidates.map(x=>x.split('-').pop()).join(', ')||'없음'}</div>`}else if(g.reason)s+=`<p>${esc(g.reason)}</p>`;return s}
function side(){const p=D.pages[cur];const T=[['calls','입력·호출'],['regions','영역 '+p.regions.length],['unassigned','미배정 '+p.unassigned.filter(u=>!u.empty).length],['issues','문제 '+p.issues.length]];
const ne=p.unassigned.filter(u=>!u.empty),cnt=r=>ne.filter(u=>r.includes(u.reason)).length,g=p.gold;
const sum=p.status!=='READY'?'':`<table class="mono" style="margin-top:6px"><tr><th>영역</th><td>${p.regions.length} (표 후보 ${p.regions.filter(r=>r.table).length}, 원시 중첩 ${p.overlaps.length}쌍)</td></tr>
<tr><th>OCR(글자 있음)</th><td>배정 ${p.assigned.filter(a=>!a.empty).length} (허용 오차 ${p.assigned.filter(a=>!a.empty&&!a.exact).length}) · 미배정 ${ne.length} = 밖 ${cnt(['outside_layout'])} / 부분 ${cnt(['partial_overlap'])} / 모호 ${cnt(['ambiguous_multiple_regions','ambiguous_overlapping_regions'])}</td></tr>
<tr><th>분할 검증</th><td>${p.partition.passed?'통과':'실패'} (OCR ${p.partition.ocr_items} = 배정 ${p.partition.assigned} + 미배정 ${p.partition.unassigned})</td></tr>
<tr><th>정답 줄 기하</th><td>${g.evaluated?`단일 ${g.geometry.single||0} · 복수 ${g.geometry.multiple||0} · 일부 ${g.geometry.partial||0} · 미포함 ${g.geometry.none||0} / ${g.required}`:'평가 불가'}</td></tr>
<tr><th>정답 줄 배정 정책</th><td>${g.evaluated?(g.policy.covered||0)+'/'+g.required+' 단일 배정':'평가 불가'}</td></tr>
<tr><th>품질 상태</th><td>${p.quality?p.quality.status:'-'} (READY≠품질 통과)</td></tr><tr><th>최종 문구</th><td>미판독(VLM 평가는 별도 화면)</td></tr></table>`;
document.getElementById('side').innerHTML=`<section><h2>${p.id} · ${esc(p.source)}</h2><div class="mute">${esc(p.basis)}</div>${sum}<div class="tabs" style="margin-top:6px">${T.map(([k,n])=>`<button class="${tab==k?'on':''}" onclick="tab='${k}';side()">${n}</button>`).join('')}</div></section><section>${{calls,regions,unassigned,issues}[tab](p)}</section>`}
function go(i){cur=i;sel=null;const p=D.pages[i];document.getElementById('img').src=p.img;nav();draw();side();history.replaceState(null,'','#'+p.id)}
// #page_id 또는 #page_id/선택ID 로 특정 사례를 바로 연다.
function fromHash(){const [pid,sid]=decodeURIComponent(location.hash.slice(1)).split('/');const i=D.pages.findIndex(p=>p.id===pid);
go(i<0?0:i);if(i>=0&&sid){pick(D.pages[i].regions.some(r=>r.id===sid)?'region':'ocr',sid);history.replaceState(null,'','#'+pid+'/'+sid)}}
tools();fromHash();
</script></body></html>
"""
