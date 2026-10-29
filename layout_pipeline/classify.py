"""상품·템플릿 분류와 영역 배정(의미 단계 A~C, docs/14).

입력은 저장된 파싱 결과(layout-result/2)의 최종 text·bbox·페이지 순서·기존 표 결과와, 영역 상자를 그린 페이지(긴 이미지는
타일 구간) 사본이다. 영역 crop 을 다시 읽지 않는다.

- 중립 별칭: 파일 F1…, 페이지 P1…, 영역 P1-R01…(페이지 안 읽기 순서). 파일명·file_id·submission_id·manifest 설명·
  평가 정답은 요청에 넣지 않는다. 응답의 별칭은 코드가 실제 ID 로 되돌린다.
- 관측 단위(A): 작은 묶음은 묶음 전체를 한 요청으로(A~C 한 번), 큰 묶음은 페이지마다 관측한 뒤(병렬) 통합 요청(B)에서
  같은 상품을 잇고 템플릿을 정한다. 영역 배정(C)은 관측 응답의 영역 배정을 상품으로 옮긴 것이다(추가 호출 없음).
- 모델 응답은 ID·분류값·최소 근거(특징 ID·영역·인용)만 받는다. 인용은 그 영역의 최종 text 또는 기존 표 셀에 그대로
  들어 있어야 근거로 인정한다(공백·따옴표·가운뎃점 표기만 맞춘다. 부분 겹침은 인정하지 않는다).
- 코드는 모델 판단을 규칙으로 바꾸지 않는다. 근거 없음·충돌·결정 신호 미사용 같은 것은 짧은 검토 코드로만 남긴다.
  퇴직연금 금융투자상품 포함/미노출은 묶음 전체 관측의 운용상품 근거와 페이지 처리 상태로 코드가 정한다.
"""
from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

from . import semantic_assets as A
from .read_stage import reading_order
from .vlm_tasks import COLORS, FONT

# observe/2(2026-10-27 E1, docs/14): 상품명은 상품명 부분만, 템플릿마다 핵심 특징을 먼저, 대상 하나뿐인 광고에서도 대상의
# 내용은 specific 이고 shared 는 공통 요소에만 쓴다는 지시를 더했다(observe/1 은 isa01 에서 '…(ISA) 소개'를 이름으로,
# ISA 설명 영역 다수를 shared 로 냈다).
# observe/3(E2a): 영역 배정을 영역 별칭 키 객체로(반복·누락 방지), 회사·비교표 유형은 대상이 아님, 범위 밖 대상도 근거(other.product)
# 필수, unknown 은 정말 판단할 수 없을 때만(observe/2 는 g02 비교표의 신탁형·일임형을 대상으로, g01·g03 에 근거 없는 대상을 냈다).
# → isa01 에서 targets=[] 인데 영역은 t1 을 가리키는 응답(실패, 기록 isa-general-2026-10-27-s3).
# observe/4(E2b): '근거 없는 대상은 만들지 않는다' 문장을 빼고(코드가 근거 없는 대상을 버린다) 비교 지시를 긍정형으로,
# 묶음 전체 관측이면 대상 1개 이상(minItems). 페이지 관측은 대상 없는 페이지가 있을 수 있어 0개 허용.
# observe/5(E2c): 페이지 관측도 대상 1개 이상. observe/4 는 g06 상품 상세 2쪽에서 대상을 선언하지 않고 영역은 t1 을 가리켰다.
# 근거 없이 채운 대상은 검사에서 버려지므로(dropped_targets) 근거 없는 상품은 생기지 않는다.
# observe/6(E3): 퇴직연금 대상은 페이지의 제도 구분·실제 소개된 운용상품 종류·고지 속 언급을 근거로 빠짐없이 쓰게 했다(observe/5 는
# tmp 퇴직연금 ETF 라인업 쪽에서 pension.common 만 냈고, 합성 '마지막 쪽 ETF'·'국채·ELS·RP 만' 사례를 미노출로 냈다).
# observe/7(E4): '이름·조건을 갖춘 별도 소개 상품은 대상' 과 '퇴직연금 운용상품은 근거' 지시의 충돌을 풀었다: 계좌·제도 광고 안의
# 편입·운용 상품은 이름·금리가 있어도 독립 대상이 아니다(observe/6 은 tmp 안내장의 GIC·ELB·정기예금 라인업을 별도 대상으로 냈다).
OBSERVE_VERSION = "observe/7"
# integrate/2(E3): 서로 다른 파일이 각각 완결된 별도 광고면 따로 두고 한 광고의 탭·이어지는 화면이면 묶으라는 지시와, 대상마다
# 그 대상 전용 짧은 영역 문구(제목 후보, 60자 이하·최대 3개, 자르지 않음)를 더했다(integrate/1 은 tmp 퇴직연금 광고 5개를 한 상품으로).
# integrate/3(E5): 응답을 대상 번호 키 객체(대상 → 상품 번호) + 상품 번호별 템플릿으로 바꿨다(integrate/2 는 tmp 에서 T1 하나만
# 쓰고 28개를 빠뜨렸다). 템플릿을 쓰지 않은 상품 번호는 멤버 제안 템플릿 + integration_templates_missing 검토 표시.
# integrate/4(E6): integrate/3 의 '별도 광고면 파일별로 따로' 문장을 빼고 탭·이어지는 화면 설명을 구체화했다(integrate/3 은 g06 한 상품의
# 탭 4개를 상품 4개로 나눴다). 서로 다른 광고를 묶었을 때는 검토 코드(pension_exposure_differs_by_file·member_names_differ)로 드러난다.
INTEGRATE_VERSION = "integrate/4"
SCOPES = ("specific", "shared", "mixed", "unknown")
MAX_TARGETS = 8
IMAGE_MAX_PIXELS = 1_200_000        # 분류 이미지는 배치 확인용(문구는 최종 text 로 준다)
# 묶음 전체를 한 요청으로 관측하는 상한. 넘으면 페이지별 관측 + 통합.
SINGLE_UNIT_LIMITS = {"pages": 4, "regions": 80, "chars": 16000, "images": 6}
READ_PAGE_STATUSES = ("READY", "CANDIDATE_RUN")
# 같은 상품에 함께 둘 수 있는 템플릿 조합. 예금성 혼합은 보통 별도 상품이라 한 상품에 둘이면 검토 표시.
ALLOWED_COMBOS = ({"loan_broker", "loan_named"},)
DEPOSIT_CODES = {"deposit_demand", "deposit_term", "deposit_saving"}

_CANON = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "・": "·", "ㆍ": "·", "•": "·",
                        "∙": "·", "‐": "-", "‑": "-", "–": "-", "—": "-", "−": "-", "〜": "~"})


def canon(s: str | None) -> str:
    """인용 비교 열쇠: NFKC·따옴표/가운뎃점/줄표 표기 통일·공백 제거. 글자 자체는 바꾸지 않는다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").translate(_CANON))


def quote_where(quote: str | None, region: dict) -> dict | None:
    """인용이 그 영역의 최종 text 나 기존 표 셀(또는 한 행)에 통째로 들어 있으면 위치, 아니면 None."""
    q = canon(quote)
    if not q:
        return None
    hit = {"text": q in canon(region.get("text")), "cells": [], "rows": []}
    table = region.get("table") or {}
    for c in table.get("cells") or []:
        if q in canon(c["text"]):
            hit["cells"].append([c["row"], c["col"]])
    for i, row in enumerate(table.get("rows") or []):
        if not table.get("cells"):
            hit["cells"] += [[i, j] for j, cell in enumerate(row) if q in canon(cell)]
        if q in canon(" ".join(row)):
            hit["rows"].append(i)
    return hit if hit["text"] or hit["cells"] or hit["rows"] else None


# ---- 묶음과 별칭 ----

def build_bundle(docs: list[dict], doc_paths: dict[str, str]) -> dict:
    """제출 묶음 하나(파일별 layout-result/2 문서들)의 파일·페이지·영역을 입력 순서로 모으고 중립 별칭을 붙인다."""
    files, pages, regions = [], [], {}
    for d in sorted(docs, key=lambda d: d["source"]["input_order"]):
        fa = f"F{len(files) + 1}"
        fdir = os.path.dirname(doc_paths[d["source"]["file_id"]])
        pas = []
        for p in d["pages"]:
            pa = f"P{len(pages) + 1}"
            ras = []
            for k, r in enumerate(reading_order([r for r in d["regions"] if r["page_id"] == p["page_id"]]), 1):
                ra = f"{pa}-R{k:02d}"
                regions[ra] = {"alias": ra, "page": pa, "file": fa, "region_id": r["region_id"], "bbox": r["bbox"],
                               "text": r["text"], "text_status": r["text_status"],
                               "table": r["table"] if r["table"] and r["table"]["status"] == "table" else None}
                ras.append(ra)
            tiles = [t["crop"] for t in (p.get("boundary") or {}).get("tiles") or []] if p["route"] == "long" else []
            pages.append({"alias": pa, "file": fa, "file_id": d["source"]["file_id"], "page_id": p["page_id"],
                          "page_no": p["page_no"], "of": len(d["pages"]), "width": p["width"], "height": p["height"],
                          "status": p["execution_status"], "image": os.path.normpath(os.path.join(fdir, p["image"]["path"])),
                          "segments": tiles or [[0, 0, p["width"], p["height"]]], "regions": ras,
                          "unassigned": [u["ocr_text"] for u in d["unassigned_texts"]
                                         if u["page_id"] == p["page_id"] and not u["empty_text"]]})
            pas.append(pa)
        files.append({"alias": fa, "file_id": d["source"]["file_id"], "pages": pas})
    return {"files": files, "pages": {p["alias"]: p for p in pages}, "page_order": [p["alias"] for p in pages],
            "regions": regions, "alias_of": {r["region_id"]: a for a, r in regions.items()}}


def plan_units(b: dict) -> list[list[str]]:
    """관측 단위(페이지 별칭 목록). 영역이 있는 페이지만 관측한다."""
    pas = [pa for pa in b["page_order"] if b["pages"][pa]["regions"]]
    lim = SINGLE_UNIT_LIMITS
    size = {"pages": len(pas), "regions": sum(len(b["pages"][pa]["regions"]) for pa in pas),
            "chars": sum(len(r["text"] or "") for r in b["regions"].values()),
            "images": sum(len(b["pages"][pa]["segments"]) for pa in pas)}
    if not pas:
        return []
    return [pas] if all(size[k] <= lim[k] for k in lim) else [[pa] for pa in pas]


def marked_segment(img: Image.Image, seg: list, regions: list[dict]) -> Image.Image:
    """페이지(또는 긴 이미지 타일 구간) 사본에 영역 상자와 번호(R01…)를 그린다. 배치 확인용으로 줄인다."""
    x0, y0, x1, y1 = seg
    crop = img.crop((x0, y0, x1, y1))
    f = min(1.0, (IMAGE_MAX_PIXELS / (crop.width * crop.height)) ** 0.5)
    if f < 1:
        crop = crop.resize((max(1, round(crop.width * f)), max(1, round(crop.height * f))), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    lw = max(2, crop.width // 400)
    font = ImageFont.truetype(FONT, max(13, crop.width // 55))
    for k, r in enumerate(regions):
        b = r["bbox"]
        if b[3] <= y0 or b[1] >= y1:
            continue
        box = [(b[0] - x0) * f, (max(b[1], y0) - y0) * f, (b[2] - x0) * f, (min(b[3], y1) - y0) * f]
        color = COLORS[k % len(COLORS)]
        d.rectangle(box, outline=color, width=lw)
        label = r["alias"].split("-", 1)[1]
        ty = box[1] - font.size - 2 if box[1] - font.size - 2 >= 0 else box[1] + lw
        d.rectangle([box[0], ty, box[0] + d.textlength(label, font=font) + 4, ty + font.size + 2], fill=color)
        d.text((box[0] + 2, ty), label, font=font, fill=(255, 255, 255))
    return crop


def composition(b: dict, observed: dict[str, str] | None = None) -> str:
    """묶음 구성 줄: 파일별 페이지 별칭과 처리 상태(중립 표현)."""
    def state(pa):
        p = b["pages"][pa]
        if p["status"] not in READ_PAGE_STATUSES:
            return "미처리"
        if not p["regions"]:
            return "영역 없음"
        return (observed or {}).get(pa, f"영역 {len(p['regions'])}개")
    return "\n".join(f"{f['alias']}: " + ", ".join(f"{pa}({state(pa)})" for pa in f["pages"]) for f in b["files"])


def region_items(b: dict, aliases: list[str], extra: dict[str, dict] | None = None, bbox: bool = True) -> str:
    """요청에 넣는 영역 목록(한 줄에 한 영역 JSON). text 는 생략 없이, 표는 기존 rows 를 함께."""
    lines = []
    for a in aliases:
        r = b["regions"][a]
        it = {"id": a} | ({"bbox": [round(v) for v in r["bbox"]]} if bbox else {}) | {"text": r["text"]}
        if r["text"] is None:
            it["status"] = "판독 실패"
        if r["table"] and r["table"].get("rows"):
            it["rows"] = r["table"]["rows"]
        it |= (extra or {}).get(a, {})
        lines.append(json.dumps(it, ensure_ascii=False))
    return "\n".join(lines)


def policy_text() -> str:
    P = A.policy()
    lines = []
    for t in P["templates"]:
        lines.append(f"- {t['code']} ({t['template_id']})")
        lines += [f"  · {f['id']}: {f['desc']}" for f in t["features"]]
        lines += [f"  판단: {x}" for x in t["rules"]] + [f"  제외: {x}" for x in t["exclusions"]]
        lines += [f"  혼합: {x}" for x in t["mixed"]]
    lines += [f"- {k}: {v}" for k, v in P["special_codes"].items()]
    return "\n".join(lines)


def _rules() -> str:
    return "\n".join(f"- {x}" for x in A.policy()["global_rules"])


# ---- A: 관측 ----

OBSERVE_RULES = (
    "당신은 금융 광고 묶음의 광고 대상·템플릿 분류기입니다. 법규 준수·적정성은 판단하지 않습니다.\n"
    "[입력]\n"
    "- 영역 목록의 text 는 이미 확정된 최종 판독 문구이고 rows 는 기존 표 판독 결과입니다. 문구를 다시 읽거나 고치지 "
    "않습니다. text=null 은 판독에 실패한 영역입니다.\n"
    "- 이미지는 페이지(긴 페이지는 세로 구간)에 영역 상자와 번호(R01…)를 그린 사본으로, 배치·소속 확인용입니다. 이미지의 "
    "R 번호는 그 이미지 페이지의 영역 ID(P번호-R번호)입니다. bbox 는 페이지 픽셀 좌표(x0,y0,x1,y1)입니다.\n"
    "[1. 문구의 역할 구별] 광고 대상을 정하기 전에 상품처럼 보이는 문구의 역할을 구별합니다.\n"
    "- 광고 대상(targets): 이 광고가 가입·이용을 권하며 실제로 알리는 상품. 자체 이름·조건을 갖춰 독립적으로 소개되는 다른 "
    "상품도 광고 대상입니다. 단, 퇴직연금·ISA 같은 계좌·제도 광고 안에서 그 안에 편입·운용할 수 있는 상품으로 소개된 것은 "
    "상품명·금리·수익률이 있어도 독립 대상이 아닙니다(퇴직연금은 pension.option_* 근거, ISA 는 holding). 회사·은행·브랜드 "
    "자체는 광고 대상이 아닙니다(회사명 영역은 shared).\n"
    "- 광고 대상이 아닌 언급(mentions): {roles}. 한 상품의 유형별 비교·차이 설명은 그 상품 하나의 내용이며 유형마다 대상을 "
    "따로 만들지 않습니다(comparison). 헷갈릴 만한 언급만 대표로 씁니다.\n"
    "- 퇴직연금 운용상품(펀드·ETF·ELB 등)은 광고 대상으로 만들지 않고 퇴직연금 대상의 evidence 에 pension.option_* 로 씁니다.\n"
    "[2. 광고 대상 targets]\n"
    "- tid: t1, t2 … (이 응답 안에서만 쓰는 번호).\n"
    "- name: 영역 text 에 적힌 상품명 부분만 그대로 복사합니다(제목·문장의 '소개'·'안내'·'출시'·'이벤트' 같은 앞뒤 말은 "
    "넣지 않음). 이름이 없으면 null. 이름을 만들거나 고치지 않습니다.\n"
    "- group: 상품군. 퇴직연금·ISA 는 투자성입니다.\n"
    "- templates: 아래 [템플릿 기준]의 코드. 한 상품에 여러 템플릿이 적용될 수 있습니다. 범위 밖 유형은 [\"unsupported\"], "
    "판단할 근거가 부족하면 [\"undetermined\"].\n"
    "- evidence: 판단에 쓴 특징마다 대표 근거 1개: feature(특징 ID), region(그 문구가 있는 영역 ID), quote(그 영역 text 또는 "
    "rows 셀에서 그대로 복사한 40자 이내 인용). 템플릿마다 그 템플릿을 고른 핵심 특징을 먼저 씁니다. unsupported·undetermined "
    "대상은 other.product 로 그 대상의 근거를 씁니다. 다른 영역 문구나 기준 예시를 인용하지 않습니다.\n"
    "- 퇴직연금 대상은 템플릿 근거와 별도로, 이 요청의 페이지에서 확인되는 제도 구분마다 pension.plan_*, 실제로 소개된 운용상품 "
    "종류마다 pension.option_*(펀드·ETF·ELB·국채/ELS/RP·원리금보장상품), 일반 고지 속 금융투자상품 언급은 pension.notice_investment "
    "근거를 빠짐없이 씁니다.\n"
    "[3. 영역 배정 regions] 영역 목록의 모든 영역 ID 마다 scope 와 targets 를 씁니다. 영역을 나누거나 합치지 않습니다.\n"
    "- specific: 한 광고 대상의 내용(설명·조건·혜택·예시·비교·표·그 대상의 고지) → targets 에 그 대상 하나. 광고 대상이 "
    "하나뿐인 광고에서도 대상의 내용은 specific 입니다.\n"
    "- shared: 특정 대상의 설명이 아닌 공통 요소(회사명·로고·머리글·바닥글·메뉴·연락처·여러 대상에 함께 적용되는 고지·"
    "심의번호 등) → targets 에 공통으로 적용되는 대상들(이 요청에 광고 대상이 없으면 빈 배열).\n"
    "- mixed: 한 영역 안에 서로 다른 광고 대상의 내용이 나뉘어 있음 → targets 에 그 대상들, parts 에 대상별로 그 영역에서 "
    "복사한 짧은 인용.\n"
    "- unknown: 어느 대상의 내용인지 정말 판단할 수 없을 때만 → targets 빈 배열. 로고·슬로건·다운로드·목록 버튼·글자 없는 "
    "장식 같은 화면 요소는 shared 입니다.\n"
)


def observe_schema(region_ids: list[str]) -> dict:
    tids = [f"t{i}" for i in range(1, MAX_TARGETS + 1)]
    codes = A.template_codes() + list(A.SPECIAL_CODES)
    ev = {"type": "object", "additionalProperties": False, "required": ["feature", "region", "quote"],
          "properties": {"feature": {"type": "string", "enum": A.feature_ids()},
                         "region": {"type": "string", "enum": region_ids}, "quote": {"type": "string"}}}
    target = {"type": "object", "additionalProperties": False, "required": ["tid", "name", "group", "templates", "evidence"],
              "properties": {"tid": {"type": "string", "enum": tids}, "name": {"type": ["string", "null"]},
                             "group": {"type": "string", "enum": A.policy()["groups"]},
                             "templates": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "enum": codes}},
                             "evidence": {"type": "array", "maxItems": 12, "items": ev}}}
    mention = {"type": "object", "additionalProperties": False, "required": ["role", "region", "quote"],
               "properties": {"role": {"type": "string", "enum": list(A.policy()["mention_roles"])},
                              "region": {"type": "string", "enum": region_ids}, "quote": {"type": "string"}}}
    # 영역 배정은 영역 별칭을 키로 한 객체: 디코더가 영역마다 정확히 한 번씩 쓰게 한다(배열이면 같은 ID 반복·누락이 생겼다).
    reg = {"type": "object", "additionalProperties": False, "required": ["scope", "targets"],
           "properties": {"scope": {"type": "string", "enum": list(SCOPES)},
                          "targets": {"type": "array", "maxItems": MAX_TARGETS, "items": {"type": "string", "enum": tids}}}}
    part = {"type": "object", "additionalProperties": False, "required": ["region", "target", "quote"],
            "properties": {"region": {"type": "string", "enum": region_ids}, "target": {"type": "string", "enum": tids},
                           "quote": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["targets", "mentions", "regions", "parts"],
            "properties": {"targets": {"type": "array", "minItems": 1, "maxItems": MAX_TARGETS, "items": target},
                           "mentions": {"type": "array", "maxItems": 10, "items": mention},
                           "regions": {"type": "object", "additionalProperties": False, "required": list(region_ids),
                                       "properties": {a: reg for a in region_ids}},
                           "parts": {"type": "array", "maxItems": 2 * len(region_ids), "items": part}}}


def observe_request(b: dict, unit: list[str], whole: bool) -> dict:
    """관측 요청: 영역 표시 이미지 + 영역 목록(별칭·bbox·최종 text·표 rows) + 분류 기준. 파일명·ID 는 넣지 않는다."""
    images, roles = [], []
    for pa in unit:
        p = b["pages"][pa]
        page_img = Image.open(p["image"]).convert("RGB")
        regs = [b["regions"][a] for a in p["regions"]]
        for seg in p["segments"]:
            images.append(marked_segment(page_img, seg, regs))
            roles.append(f"{pa} y {seg[1]}–{seg[3]}" if len(p["segments"]) > 1 else pa)
    aliases = [a for pa in unit for a in b["pages"][pa]["regions"]]
    n_pages = len(b["page_order"])
    if whole:
        scope = (f"이 요청은 광고 묶음 전체({n_pages}페이지)입니다. 같은 상품이 여러 페이지·파일에 이어지면 하나의 광고 대상으로 씁니다. "
                 "퇴직연금의 금융투자상품 포함 여부는 모든 페이지를 보고 판단합니다.")
    else:
        pa = unit[0]
        p = b["pages"][pa]
        scope = (f"이 요청은 광고 묶음 {n_pages}페이지 중 {pa}({p['file']}의 {p['page_no']}/{p['of']}쪽) 한 페이지입니다. 이 페이지에서 "
                 "보이는 근거로만 씁니다. 다른 페이지와의 연결과 묶음 전체 판단은 다음 단계에서 합니다. 이 페이지에 상품 이름이 "
                 "없어도 광고 대상의 내용이 있으면 이름 없는 대상(name=null)으로 씁니다.")
    head = OBSERVE_RULES.replace("{roles}", ", ".join(f"{k}({v})" for k, v in A.policy()["mention_roles"].items()))
    text = (head + "[템플릿 기준]\n" + policy_text() + "\n[주의]\n" + _rules()
            + f"\n[묶음 구성]\n{composition(b)}\n{scope}\n"
            + "[이미지] " + "; ".join(f"{i + 1}: {r}" for i, r in enumerate(roles))
            + f"\n[영역 목록] {len(aliases)}개\n" + region_items(b, aliases))
    return {"version": OBSERVE_VERSION, "pages": unit, "whole": whole, "regions": aliases, "images": images,
            "image_roles": roles, "text": text, "schema": observe_schema(aliases),
            "max_tokens": min(16000, 1500 + 70 * len(aliases))}


def _codes(raw: list[str], errors: list[str], where: str) -> list[str]:
    codes = list(dict.fromkeys(raw or []))
    special = [c for c in codes if c in A.SPECIAL_CODES]
    if special and len(special) < len(codes):
        errors.append(f"{where}: 특수 코드 {special} 와 템플릿을 함께 씀(특수 코드 제외)")
        codes = [c for c in codes if c not in A.SPECIAL_CODES]
    if len(special) > 1 and len(special) == len(codes):
        codes = ["undetermined"]
    return codes or ["undetermined"]


def validate_observe(call, req: dict, b: dict) -> dict:
    """관측 응답 검사. 구조가 깨지면 ok=False(그 단위 영역은 배정 실패). 인용은 영역 text·표 셀에서 확인해 valid 로 표시."""
    out = {"ok": False, "errors": [], "targets": [], "mentions": [], "regions": {}, "parts": [], "missing": []}
    if call.error or not isinstance(call.parsed, dict):
        out["errors"].append(call.error or "응답 없음")
        out["missing"] = list(req["regions"])
        return out
    p, unit = call.parsed, set(req["regions"])
    tids = []
    for t in p.get("targets") or []:
        if t["tid"] in tids:
            out["errors"].append(f"{t['tid']}: 대상 번호 중복(뒤 항목 무시)")
            continue
        tids.append(t["tid"])
        ev = []
        for e in t.get("evidence") or []:
            where = quote_where(e["quote"], b["regions"][e["region"]]) if e["region"] in unit else None
            ev.append({"feature": e["feature"], "region": e["region"], "quote": e["quote"], "valid": where is not None,
                       "where": where})
        name = (t.get("name") or "").strip() or None
        target = {"tid": t["tid"], "name": name, "group": t["group"],
                  "templates": _codes(t["templates"], out["errors"], t["tid"]), "evidence": ev}
        texts = [canon(b["regions"][a]["text"]) for a in unit if b["regions"][a]["text"]]
        if not any(e["valid"] for e in ev) and not (name and any(canon(name) in x for x in texts)):
            # 근거 인용도 영역 문구에 있는 이름도 없는 대상은 상품으로 등록하지 않는다(trace 에만 남김)
            out.setdefault("dropped_targets", []).append(target | {"why": "no_name_or_valid_evidence"})
            tids.remove(t["tid"])
            continue
        out["targets"].append(target)
    for m in p.get("mentions") or []:
        out["mentions"].append({"role": m["role"], "region": m["region"], "quote": m["quote"],
                                "valid": quote_where(m["quote"], b["regions"][m["region"]]) is not None})
    dropped = {t["tid"] for t in out.get("dropped_targets", [])}
    items = p.get("regions") or {}
    undeclared = sorted({t for x in (items.values() if isinstance(items, dict) else []) for t in x["targets"]}
                        - set(tids) - dropped)
    if undeclared:
        out["errors"].append(f"targets 에 없는 대상 {undeclared} 를 영역 배정이 가리킴(그 배정은 미확정)")
    for a in req["regions"]:
        item = items.get(a) if isinstance(items, dict) else None
        if item is None:
            continue
        ts = [t for t in dict.fromkeys(item["targets"]) if t in tids]
        scope, note = item["scope"], None
        if scope == "unknown":
            ts = []
        elif scope in ("specific", "mixed") and not ts and len(live := [t for t in tids if t not in dropped]) == 1:
            # 관측 단위에 남은 대상이 하나뿐이면, 상품 전용·혼합인데 대상을 비웠거나 버려진 대상(근거·이름 없음)만 가리킨 영역은 그 대상밖에
            # 될 수 없다(docs/19, whole-test 대출 광고2 2쪽: 보증료할인 영역을 근거 없는 두 번째 대상에 묶은 응답).
            scope, ts = "specific", live
            note = "dropped_target_filled_single_target" if set(item["targets"]) & dropped else "specific_filled_single_target"
        elif scope == "specific" and len(ts) != 1:
            scope, note = ("mixed", "specific_multi") if ts else (
                "unknown", "dropped_target" if set(item["targets"]) & dropped else "specific_without_target")
        elif scope == "mixed" and len(ts) < 2:
            scope, note = ("specific", "mixed_single") if ts else (
                "unknown", "dropped_target" if set(item["targets"]) & dropped else "mixed_without_target")
        out["regions"][a] = {"scope": scope, "targets": ts, "note": note}
    out["missing"] = [a for a in req["regions"] if a not in out["regions"]]
    for x in p.get("parts") or []:
        if x["target"] in tids:
            out["parts"].append({"region": x["region"], "target": x["target"], "quote": x["quote"],
                                 "valid": quote_where(x["quote"], b["regions"][x["region"]]) is not None})
    out["ok"] = True
    return out


# observe 모드의 '상품 ID 와 영역 연결 검증' 뒤 정리(docs/16 §6.4·§9). 작은 묶음 legacy 관측 응답 55개 오프라인 평가에서 기대 상품 수와
# 같은 응답이 45 → 53(남은 오류는 회사명·운용상품을 이름 있는 대상으로 낸 2건).
TARGET_RULES_VERSION = "unnamed-target-rules/2"     # /2: + R3(docs/20)


def _tid_no(tid: str) -> int:
    return int(tid[1:]) if tid[1:].isdigit() else 1 << 30


def apply_target_rules(obs: dict) -> dict:
    """관측 결과(validate_observe)의 이름 없는 대상 정리. 문구·영역은 바꾸지 않고 대상과 영역 소속만 정리한다.

    R1: 이름이 없고 그 대상만의 영역(scope=specific 로 그 대상 하나)이 없는 대상은 상품이 아니다. 그 대상을 가리키던 공유·혼합 표시는
        남은 대상으로 둔다(g01 고정·변동 금리 표 칸, tt05 영역 없는 unsupported 대상).
    R2: 이름 없는 대상의 템플릿 묶음이 같은 관측의 다른 대상과 같으면 같은 상품으로 합친다(이름 있는 대상 우선, 없으면 먼저 나온 이름
        없는 대상). 참고 구현의 '이름이나 종류가 다를 때만 새 상품'. 합친 대상의 근거는 남은 대상에 더한다(t09 같은 퇴직연금 대상 2개).
    R3: 이름·상품명·유형 근거 없이 영역 하나의 문구만 근거인 대상은 같은 관측의 유일한 이름 있는 대상에 합친다(아래 설명).
    정리 내역은 target_rules 에, R1 로 뺀 대상은 dropped_targets 에 남긴다."""
    if not obs.get("ok"):
        return obs
    obs = copy.deepcopy(obs)
    actions = []
    for t in list(obs["targets"]):
        if t["name"]:
            continue
        tid = t["tid"]
        key = sorted(set(t["templates"]))
        same = sorted((o for o in obs["targets"] if o["tid"] != tid and sorted(set(o["templates"])) == key),
                      key=lambda o: (o["name"] is None, _tid_no(o["tid"])))
        into = same[0]["tid"] if same and (same[0]["name"] or _tid_no(same[0]["tid"]) < _tid_no(tid)) else None
        own = [a for a, x in obs["regions"].items() if x["scope"] == "specific" and x["targets"] == [tid]]
        if into is None and own:
            continue
        rule = "R2" if into else "R1"
        if into:
            keep = next(o for o in obs["targets"] if o["tid"] == into)
            keep["evidence"] += [e for e in t["evidence"] if e not in keep["evidence"]]
        else:
            obs.setdefault("dropped_targets", []).append(t | {"why": "R1_unnamed_without_own_regions"})
        _drop_target(obs, tid, into, rule)
        actions.append({"rule": rule, "tid": tid, "into": into, "templates": t["templates"]})
    # R3(docs/20): 이름도 상품명·유형 근거도 없이 영역 하나의 문구만 근거로 생긴 대상은 별도 상품이 아니다. 같은 관측에 이름 있는
    # 대상이 하나뿐이면 그 대상에 합친다(템플릿은 이름 있는 대상 그대로). whole-test 적립식 광고 2: 적금 유의사항('이 예금은
    # 예금자보호법에 따라…')을 퇴직연금 유의 특징으로 인용한 이름 없는 대상이 그 유의사항 영역을 가져갔다.
    named = [o for o in obs["targets"] if o["name"]]
    for t in [o for o in obs["targets"] if not o["name"]] if len(named) == 1 else []:
        valid = [e for e in t["evidence"] if e.get("valid")]
        weak = len({e["region"] for e in valid}) <= 1 and not any(e["feature"].rsplit(".", 1)[-1] in IDENTITY_FEATURES for e in valid)
        if weak and sorted(set(t["templates"])) != sorted(set(named[0]["templates"])):
            into = named[0]["tid"]
            named[0]["evidence"] += [e for e in t["evidence"] if e not in named[0]["evidence"]]
            _drop_target(obs, t["tid"], into, "R3")
            actions.append({"rule": "R3", "tid": t["tid"], "into": into, "templates": t["templates"]})
    obs["target_rules"] = actions
    return obs


IDENTITY_FEATURES = {"name", "type"}      # R3: 상품을 따로 세우는 근거(상품명·상품 유형) 특징 이름


def _drop_target(obs: dict, tid: str, into: str | None, rule: str) -> None:
    """대상 tid 를 관측에서 빼고 그 대상을 가리키던 영역·부분 인용을 into 로 옮긴다(into 가 없으면 지운다)."""
    obs["targets"] = [o for o in obs["targets"] if o["tid"] != tid]
    for x in obs["regions"].values():
        if tid not in x["targets"]:
            continue
        x["targets"] = list(dict.fromkeys(into if y == tid else y for y in x["targets"] if into or y != tid))
        if x["scope"] == "mixed" and len(x["targets"]) == 1:
            x["scope"], x["note"] = "specific", x["note"] or f"{rule}_mixed_single"
        elif not x["targets"] and x["scope"] in ("specific", "mixed"):
            x["scope"], x["note"] = "unknown", x["note"] or f"{rule}_without_target"
    obs["parts"] = [p | {"target": into} if p["target"] == tid and into else p for p in obs["parts"] if into or p["target"] != tid]


# ---- B: 통합(페이지별 관측이 여럿일 때) ----

INTEGRATE_RULES = (
    "당신은 금융 광고 묶음의 상품 통합기입니다. 법규 준수·적정성은 판단하지 않습니다.\n"
    "페이지별로 따로 관측한 광고 대상(T번호)을 묶음 단위 상품으로 정리합니다.\n"
    "[할 일]\n"
    "- targets 에 대상 번호마다 그 대상이 속하는 상품 번호(p1, p2 …)를 씁니다. 같은 상품을 가리키는 대상은 같은 상품 번호입니다. "
    "그다음 products 에 쓰인 상품 번호마다 templates 를 씁니다.\n"
    "- 같은 파일의 이어지는 페이지에서 같은 상품의 내용을 이어 설명하는 대상, 한 광고의 탭·이어지는 화면처럼 여러 파일이 한 "
    "상품 소개를 나눠 담은 대상(상품명이 한 곳에만 있고 나머지는 금리·조건·유의사항 같은 상세)은 같은 상품입니다. 이름이 없는 "
    "대상은 근거와 위치를 보고 이어지는 상품에 묶거나, 판단할 수 없으면 따로 둡니다. 제목 후보는 대상마다 그 대상 전용 영역의 "
    "짧은 문구입니다.\n"
    "- 같은 이름이라는 이유만으로 서로 다른 상품을 합치지 않습니다. 이름이 없는 대상에 이름을 만들지 않습니다.\n"
    "- 상품마다 templates 를 멤버들의 근거 전체를 보고 정합니다(아래 기준). 퇴직연금의 운용상품 소개는 모든 멤버 페이지를 "
    "합쳐 봅니다. 운용상품(펀드·ETF·ELB 등)만 소개한 페이지의 대상이 퇴직연금 광고의 일부이면 그 퇴직연금 상품에 묶습니다.\n"
)


def integrate_schema(target_ids: list[str]) -> dict:
    """응답: {"targets": {T번호: {"product": p번호}}, "products": [{product, templates}]}. 대상마다 정확히 한 번(키)."""
    codes = A.template_codes() + list(A.SPECIAL_CODES)
    pids = [f"p{i}" for i in range(1, len(target_ids) + 1)]
    tgt = {"type": "object", "additionalProperties": False, "required": ["product"],
           "properties": {"product": {"type": "string", "enum": pids}}}
    prod = {"type": "object", "additionalProperties": False, "required": ["product", "templates"],
            "properties": {"product": {"type": "string", "enum": pids},
                           "templates": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "enum": codes}}}}
    return {"type": "object", "additionalProperties": False, "required": ["targets", "products"],
            "properties": {"targets": {"type": "object", "additionalProperties": False, "required": list(target_ids),
                                       "properties": {t: tgt for t in target_ids}},
                           "products": {"type": "array", "minItems": 1, "maxItems": len(target_ids), "items": prod}}}


def global_targets(units: list[dict]) -> dict[str, dict]:
    """관측 단위의 대상(t번호)을 묶음 번호 T1… 로 모은다(단위 순서 → 응답 순서)."""
    out = {}
    for ui, u in enumerate(units):
        for t in (u["obs"]["targets"] if u["obs"]["ok"] else []):
            out[f"T{len(out) + 1}"] = t | {"unit": ui, "pages": t.get("pages", u["pages"])}
    return out


TITLE_MAX_CHARS = 60


def titles(b: dict, unit: dict, tid: str, k: int = 3) -> list[str]:
    """대상 전용(specific) 영역 중 짧은 문구(제목 후보) 최대 k개, 읽기 순서. 문구를 자르지 않고 짧은 영역만 고른다."""
    out = []
    for a in (a for pa in unit["pages"] for a in b["pages"][pa]["regions"]):
        x = unit["obs"]["regions"].get(a)
        text = (b["regions"][a]["text"] or "").strip()
        if x and x["scope"] == "specific" and x["targets"] == [tid] and 0 < len(text) <= TITLE_MAX_CHARS:
            out.append(text)
            if len(out) == k:
                break
    return out


def integrate_request(b: dict, units: list[dict], targets: dict[str, dict]) -> dict:
    observed = {}
    for u in units:
        for pa in u["pages"]:
            observed[pa] = f"영역 {len(b['pages'][pa]['regions'])}개" if u["obs"]["ok"] else "관측 실패: 근거 없음"
    lines = []
    for ta, t in targets.items():
        name = json.dumps(t["name"], ensure_ascii=False)
        lines.append(f"- {ta} [{', '.join(t['pages'])}] name={name} group={t['group']} templates={json.dumps(t['templates'])}")
        ev = [f"{e['feature']} {e['region']} {json.dumps(e['quote'], ensure_ascii=False)}" for e in t["evidence"] if e["valid"]]
        lines.append("  근거: " + ("; ".join(ev) if ev else "(인용 확인된 근거 없음)"))
        lines.append("  제목 후보: " + json.dumps(titles(b, units[t["unit"]], t["tid"]), ensure_ascii=False))
    text = (INTEGRATE_RULES + "[템플릿 기준]\n" + policy_text() + "\n[주의]\n" + _rules()
            + f"\n[묶음 구성]\n{composition(b, observed)}\n[페이지별 광고 대상] {len(targets)}개\n" + "\n".join(lines))
    ids = list(targets)
    return {"version": INTEGRATE_VERSION, "targets": ids, "proposals": {t: targets[t]["templates"] for t in ids}, "text": text,
            "schema": integrate_schema(ids), "images": [], "max_tokens": min(6000, 400 + 60 * len(ids))}


def validate_integrate(call, req: dict) -> dict:
    out = {"ok": False, "errors": [], "products": []}
    if call.error or not isinstance(call.parsed, dict):
        out["errors"].append(call.error or "응답 없음")
        return out
    items = call.parsed.get("targets") or {}
    groups: dict[str, list[str]] = {}
    for t in req["targets"]:
        x = items.get(t) if isinstance(items, dict) else None
        if x is not None:
            groups.setdefault(x["product"], []).append(t)
    tmpl = {}
    for p in call.parsed.get("products") or []:
        if p["product"] in tmpl:
            out["errors"].append(f"{p['product']}: 상품 템플릿 중복(뒤 항목 무시)")
            continue
        tmpl[p["product"]] = _codes(p["templates"], out["errors"], p["product"])
    for pid, members in groups.items():
        if pid in tmpl:
            out["products"].append({"members": members, "templates": tmpl[pid], "codes": []})
        else:                       # 상품 템플릿을 쓰지 않은 상품 번호: 멤버 대상의 제안 템플릿을 쓰고 검토 표시
            out["errors"].append(f"{pid}: products 에 템플릿 없음(멤버 대상 제안 사용)")
            proposed = list(dict.fromkeys(c for m in members for c in req["proposals"][m] if c not in A.SPECIAL_CODES))
            out["products"].append({"members": members, "templates": proposed or ["undetermined"],
                                    "codes": ["integration_templates_missing"]})
    out["missing"] = [t for t in req["targets"] if t not in {m for g in groups.values() for m in g}]
    out["ok"] = True
    return out


# ---- C: 상품 확정과 영역 배정 ----

def _name(names: list[str | None], b: dict, codes: list[str]) -> str | None:
    texts = [canon(r["text"]) for r in b["regions"].values() if r["text"]]
    valid = [n for n in names if n and any(canon(n) in t for t in texts)]
    if len({canon(n) for n in names if n}) > 1:
        codes.append("member_names_differ")
    if any(n and n not in valid for n in names):
        codes.append("name_not_in_text")
    if not valid:
        return None
    count = Counter(canon(n) for n in valid)
    best = max(count.values())
    return next(n for n in valid if count[canon(n)] == best)


# 결정 신호 검사(decisive-signal/1, docs/19): 정책에 '결정 신호:' 특징이 있는 템플릿은 그 특징으로 인용한 문구에 결정 신호 낱말이
# 실제로 있어야 유지한다. 없으면 그 템플릿을 빼고, 정책의 대체 규칙(DECISIVE_FALLBACK)이 있으면 그 템플릿으로 둔다.
# whole-test 대출 광고2: 지점 담당자 '강남중앙금융센터 류지선 과장'(보조 신호 상담사 이름)을 결정 신호로 인용해 대출모집인이 붙었다.
DECISIVE_RULES_VERSION = "decisive-signal/1"
DECISIVE_FALLBACK = {"loan_broker": "loan_named"}     # loan_named 규칙: '대출모집인 결정 신호가 없으면 이 템플릿으로 판단한다'


def decisive_terms(code: str) -> list[str]:
    """템플릿의 '결정 신호:' 특징 설명에 나열된 낱말(비교 열쇠). 그런 특징이 없으면 빈 목록."""
    t = next((t for t in A.templates() if t["code"] == code), None)
    for f in (t or {}).get("features") or []:
        if f["desc"].startswith("결정 신호:"):
            return [canon(w) for w in f["desc"].split(":", 1)[1].split(",") if canon(w)]
    return []


def _decisive_filter(codes: list[str], evidence: list[dict], review: list[str]) -> list[str]:
    out = list(codes)
    for c in [c for c in codes if decisive_terms(c)]:
        terms = decisive_terms(c)
        if any(e["valid"] and A.feature_template(e["feature"]) == c and any(t in canon(e["quote"]) for t in terms) for e in evidence):
            continue
        out = [x for x in out if x != c]
        review.append("decisive_signal_missing")
        fb = DECISIVE_FALLBACK.get(c)
        if fb and fb not in out:
            out.append(fb)
    return out


def _template_codes(codes: list[str], evidence: list[dict], review: list[str]) -> list[str]:
    codes = _decisive_filter(codes, evidence, review)
    real = [c for c in codes if c not in A.SPECIAL_CODES]
    supported = {A.feature_template(e["feature"]) for e in evidence if e["valid"]}
    if any(c not in supported for c in real):
        review.append("template_evidence_missing")
    if len(real) > 1:
        s = set(real)
        if s in ALLOWED_COMBOS:
            review.append("broker_with_named_loan")
        elif s <= DEPOSIT_CODES:
            review.append("multi_template_product")
        else:
            review.append("template_conflict")
    return codes


def _pension(product: dict, b: dict, units: list[dict], evidence: list[dict]) -> tuple[dict, list[str]]:
    """퇴직연금 금융투자상품 포함/미노출/미확정·제도·운용상품. 상품이 걸친 파일의 모든 페이지를 본다."""
    P = A.policy()["pension"]
    valid = [e for e in evidence if e["valid"]]
    comps = [c for c in ("FUND", "ETF", "ELB") if any(P["component_features"].get(e["feature"]) == c for e in valid)]
    other = any(e["feature"] in P["inclusion_only_features"] for e in valid)
    plans = [x for x in ("IRP", "DC", "DB") if any(P["plan_features"].get(e["feature"]) == x for e in valid)]
    codes = []
    observed = {pa for u in units if u["obs"]["ok"] for pa in u["pages"]}
    pages = [pa for f in b["files"] if f["file_id"] in product["files"] for pa in f["pages"]]
    incomplete = [pa for pa in pages if b["pages"][pa]["status"] not in READ_PAGE_STATUSES
                  or (b["pages"][pa]["regions"] and pa not in observed)
                  or any(b["regions"][a]["text"] is None for a in b["pages"][pa]["regions"])]
    terms = re.compile(A.policy()["review_signals"]["pension_investment_terms"], re.I)
    if comps or other:
        exposure = "included"
        if not comps:
            codes.append("pension_other_investment_only")
    elif incomplete:
        exposure = "undetermined"
        codes.append("pension_pages_incomplete")
    elif any(terms.search(t) for pa in pages for t in b["pages"][pa]["unassigned"]):
        exposure = "undetermined"
        codes.append("pension_unassigned_ocr_terms")
    else:
        exposure = "not_exposed"
    if exposure == "undetermined":
        codes.append("pension_exposure_undetermined")
    # 여러 파일에 걸친 상품인데 파일마다 운용상품 소개 여부가 다르면(별도 광고를 묶었을 수 있음) 검토 표시만 한다
    inclusion = set(P["component_features"]) | set(P["inclusion_only_features"])
    per_file = {f["file_id"]: any(e["feature"] in inclusion and b["regions"][e["region"]]["file"] == f["alias"] for e in valid)
                for f in b["files"] if f["file_id"] in product["files"]}
    if len(set(per_file.values())) > 1:
        codes.append("pension_exposure_differs_by_file")
    return {"investment_products": exposure, "plans": plans, "components": comps}, codes


FIRST_INPUT_VERSION = "first-input/1"


def first_input_rules(b: dict, targets: dict[str, dict], groups: list[dict]) -> tuple[list[dict], list[dict]]:
    """first-input/1(docs/20): 상품 그룹(관측·통합 결과)을 묶음 단위로 정리한다. 모델을 부르지 않는다.

    M1 같은 이름: 이름(canon: 공백·표기 통일)이 같은 그룹은 한 상품이다(띄어쓰기만 다른 '…동행예금'/'…동행 예금').
    M2 첫 입력 기준(파일이 둘 이상): 한 상품의 여러 입력은 첫 입력에서 상품·템플릿이 정해지고 뒤 입력은 이어지는 화면이다.
       - 첫 파일에 나온 대상이 있는 그룹의 템플릿은 첫 파일 대상들의 템플릿을 쓴다(뒤 화면만 보고 다르게 판단해도 바꾸지 않음).
       - 뒤 입력에만 있는 이름 없는 대상 그룹은 첫 입력의 상품(이름 있는 첫 그룹)에 잇는다. 이름 있는 다른 상품은 그대로 둔다.
    정리 내역(merge_rules)을 남기고 해당 상품 검토 코드(merged_same_name·template_from_first_input·unnamed_attached)를 붙인다."""
    rules: list[dict] = []
    order = {pa: i for i, pa in enumerate(b["page_order"])}
    first = lambda g: min(order.get(pa, 1 << 30) for m in g["members"] for pa in targets[m]["pages"])  # noqa: E731
    names = lambda g: {canon(targets[m]["name"]) for m in g["members"] if targets[m]["name"]}  # noqa: E731
    groups = sorted((dict(g, members=list(g["members"]), codes=list(g["codes"])) for g in groups), key=first)
    merged: list[dict] = []
    for g in groups:                                   # M1(입력 순서로: 앞 그룹이 남는다)
        into = next((k for k in merged if names(k) & names(g)), None)
        if into is None:
            merged.append(g)
            continue
        into["members"] += g["members"]
        into["codes"] = list(dict.fromkeys(into["codes"] + g["codes"] + ["merged_same_name"]))
        rules.append({"rule": "M1", "members": g["members"], "into": into["members"][0], "templates": g["templates"],
                      "kept": into["templates"]})
    if len(b["files"]) < 2:
        return merged, rules
    first_pages = set(b["files"][0]["pages"])
    in_first = lambda m: bool(set(targets[m]["pages"]) & first_pages)  # noqa: E731
    anchors = [g for g in merged if any(in_first(m) for m in g["members"])]
    if not anchors:
        return merged, rules
    for g in anchors:                                  # M2 템플릿: 첫 파일 대상들의 템플릿
        tpl = list(dict.fromkeys(c for m in g["members"] if in_first(m) for c in targets[m]["templates"] if c not in A.SPECIAL_CODES))
        if tpl and sorted(tpl) != sorted(c for c in g["templates"] if c not in A.SPECIAL_CODES):
            rules.append({"rule": "M2_template", "group": g["members"][0], "templates": g["templates"], "kept": tpl})
            g["templates"] = tpl
            g["codes"] = list(dict.fromkeys(g["codes"] + ["template_from_first_input"]))
        elif tpl:
            g["templates"] = tpl
    main = next((g for g in anchors if names(g)), anchors[0])
    out = []
    for g in merged:                                   # M2 이름 없는 뒤 입력 대상 → 첫 입력 상품
        if any(g is a for a in anchors) or names(g):
            out.append(g)
            continue
        main["members"] += g["members"]
        main["codes"] = list(dict.fromkeys(main["codes"] + g["codes"] + ["unnamed_attached"]))
        rules.append({"rule": "M2_unnamed", "members": g["members"], "into": main["members"][0], "templates": g["templates"],
                      "kept": main["templates"]})
    return out, rules


def resolve(b: dict, units: list[dict], integ: dict | None) -> dict:
    """관측(+통합) 결과 → 상품 목록과 영역별 배정. 상품 ID 는 묶음 안 첫 등장 순서(product_001…)."""
    targets = global_targets(units)
    if integ is None:                       # 단일 관측: 대상 하나 = 상품 하나
        groups = [{"members": [t], "templates": targets[t]["templates"], "codes": []} for t in targets]
    elif integ["ok"]:
        groups = [p | {"codes": list(p.get("codes", []))} for p in integ["products"]]
        groups += [{"members": [t], "templates": targets[t]["templates"], "codes": ["integration_missing_target"]}
                   for t in integ.get("missing", [])]
    else:                                   # 통합 실패: 관측 대상을 그대로 두고 검토 표시(잇지 못했을 수 있음)
        groups = [{"members": [t], "templates": targets[t]["templates"], "codes": ["integration_failed"]} for t in targets]
    groups, merge_rules = first_input_rules(b, targets, groups)
    owner = {}                              # (단위 번호, t번호) → 그룹 번호
    for gi, g in enumerate(groups):
        for m in g["members"]:
            owner[(targets[m]["unit"], targets[m]["tid"])] = gi
    # 영역 배정(그룹 번호 기준)
    assign: dict[str, dict] = {}
    for ui, u in enumerate(units):
        obs = u["obs"]
        for a in [a for pa in u["pages"] for a in b["pages"][pa]["regions"]]:
            item = obs["regions"].get(a) if obs["ok"] else None
            if item is None:
                assign[a] = {"status": "failed", "scope": None, "groups": [],
                             "codes": ["assignment_failed" if not obs["ok"] else "assignment_missing"]}
                continue
            gs = list(dict.fromkeys(owner[(ui, t)] for t in item["targets"]))
            scope = item["scope"]
            if scope == "mixed" and len(gs) == 1:      # 통합에서 같은 상품으로 묶인 대상들
                scope = "specific"
            codes = [] if scope != "unknown" or not item["note"] else [
                "target_without_evidence" if item["note"] == "dropped_target" else "assignment_invalid"]
            if item["note"] == "dropped_target_filled_single_target":
                codes.append("target_without_evidence")      # 남은 대상 하나로 채웠지만 원래 응답은 근거 없는 대상을 가리켰다(검토 표시 유지)
            if scope == "unknown":
                codes.append("assignment_unknown")
            assign[a] = {"status": "partial" if scope == "unknown" else "completed", "scope": scope, "groups": gs,
                         "codes": codes, "unit": ui}
    # 공통(shared) 영역인데 대상이 비면: 같은 페이지 → 같은 파일 → (묶음에 상품이 하나뿐이면) 그 상품
    def present(pas: list[str]) -> list[int]:
        found = [g for a in (x for pa in pas for x in b["pages"][pa]["regions"]) for g in assign.get(a, {}).get("groups", [])]
        found += [owner[(targets[m]["unit"], targets[m]["tid"])] for m in targets if set(targets[m]["pages"]) & set(pas)
                  and len(targets[m]["pages"]) == 1]
        return sorted(set(found))
    for a, x in assign.items():
        if x["scope"] == "shared" and not x["groups"]:
            pa = b["regions"][a]["page"]
            fpages = next(f["pages"] for f in b["files"] if f["alias"] == b["pages"][pa]["file"])
            gs = present([pa]) or present(fpages) or (list(range(len(groups))) if len(groups) == 1 else [])
            if gs:
                x["groups"] = gs
            else:
                x |= {"status": "partial", "scope": "unknown", "codes": x["codes"] + ["shared_without_product"]}
    # 상품 순서: 영역(없으면 근거 영역)의 첫 등장
    order = {a: i for i, a in enumerate(b["regions"])}
    first = {}
    for a, x in assign.items():
        for g in x["groups"]:
            first[g] = min(first.get(g, 1 << 30), order[a])
    for gi, g in enumerate(groups):
        if gi not in first:
            ev = [order[e["region"]] for m in g["members"] for e in targets[m]["evidence"]]
            first[gi] = (min(ev) if ev else 1 << 30) + (1 << 29)
    ranked = sorted(range(len(groups)), key=lambda gi: (first[gi], gi))
    pid = {gi: f"product_{k + 1:03d}" for k, gi in enumerate(ranked)}
    products = []
    for gi in ranked:
        g = groups[gi]
        members = [targets[m] for m in g["members"]]
        evidence = [e for m in members for e in m["evidence"]]
        review = list(g["codes"])
        codes = _template_codes(g["templates"], evidence, review)
        name = _name([m["name"] for m in members], b, review)
        regions = [a for a, x in assign.items() if gi in x["groups"]]
        spanned = {b["regions"][a]["page"] for a in regions} | {pa for m in members for pa in m["pages"]}
        files = [f["file_id"] for f in b["files"] if set(f["pages"]) & spanned]
        status = "unsupported" if codes == ["unsupported"] else "undetermined" if codes == ["undetermined"] else "classified"
        if status == "classified":
            group = A.template(codes[0])["product_group"]
            if len({A.template(c)["product_group"] for c in codes}) > 1:
                review.append("template_group_conflict")
        else:
            group = Counter(m["group"] for m in members).most_common(1)[0][0] if members else None
            review.append("template_undetermined" if status == "undetermined" else "type_unsupported")
        product = {"product_id": pid[gi], "name": name, "product_group": group, "status": status,
                   "codes": [c for c in codes if c not in A.SPECIAL_CODES], "files": files,
                   "pages": [b["pages"][pa]["page_id"] for pa in b["page_order"] if pa in spanned], "pension": None,
                   "members": g["members"], "evidence": evidence, "regions": regions, "review": review}
        if "pension" in codes:
            product["pension"], extra = _pension(product, b, units, evidence)
            product["review"] += extra
        texts = [b["regions"][a]["text"] or "" for a in regions]
        broker = re.compile(A.policy()["review_signals"]["loan_broker_decisive"])
        if status == "classified" and product_group_is_loan(codes) and "loan_broker" not in codes and any(broker.search(t) for t in texts):
            product["review"].append("broker_signal_unused")
        if any(b["pages"][b["regions"][a]["page"]]["status"] == "CANDIDATE_RUN" for a in regions):
            product["review"].append("candidate_pages")
        product["review"] = list(dict.fromkeys(product["review"]))
        products.append(product)
    # ISA 일반 상품과 ISA 유형(신탁형·일임형) 상품이 같은 파일에 함께 있으면 유형 비교를 상품으로 나눴을 수 있다(검토 표시만)
    isa_types = {"isa_trust", "isa_discretionary"}
    for p in products:
        for q in products:
            if p is not q and "isa_general" in p["codes"] and isa_types & set(q["codes"]) and set(p["files"]) & set(q["files"]):
                for x in (p, q):
                    if "isa_types_split" not in x["review"]:
                        x["review"].append("isa_types_split")
    assignments = {}
    for a, x in assign.items():
        pids = [pid[g] for g in x["groups"]]
        parts = [p | {"product_id": pid[owner[(x["unit"], p["target"])]]} for p in units[x["unit"]]["obs"]["parts"]
                 if p["region"] == a] if x["scope"] == "mixed" else []
        codes = list(x["codes"])
        if x["scope"] == "mixed" and any(not any(p["valid"] and p["product_id"] == q for p in parts) for q in pids):
            codes.append("mixed_parts_missing")       # 상품별 근거 인용이 없거나 영역 문구와 맞지 않음
        # legacy 는 관측 응답이 영역마다 배정한다(상품이 하나여도 모델 판단)
        assignments[b["regions"][a]["region_id"]] = {"status": x["status"], "scope": x["scope"], "product_ids": pids,
                                                     "basis": "model" if x["status"] in ("completed", "partial") else None,
                                                     "codes": list(dict.fromkeys(codes)), "alias": a, "parts": parts}
    return {"products": products, "assignments": assignments, "targets": targets, "merge_rules": merge_rules,
            "groups": [{"members": g["members"], "templates": g["templates"], "product_id": pid[gi]} for gi, g in enumerate(groups)]}


def product_group_is_loan(codes: list[str]) -> bool:
    return any(A.template(c)["product_group"] == "대출성" for c in codes if c not in A.SPECIAL_CODES)
