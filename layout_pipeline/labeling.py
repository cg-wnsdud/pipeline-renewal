"""영역 라벨링(의미 단계 D, docs/14): 상품·템플릿이 정해진 뒤 그 상품의 라벨 스키마로 영역마다 해당 구분값을 모두 붙인다.

- 요청에는 그 상품 템플릿의 허용 라벨·라벨 지식(템플릿 예시문구·다른 광고의 확정 예시·반례, 출처 광고 이름 없음)과
  영역의 최종 text(생략 없이)·기존 표 rows 만 넣는다. 이미지·OCR 문자열은 넣지 않는다(표를 다시 추출하지 않는다).
- 상품 소속은 바꾸지 않는다. 퇴직연금 운용상품(펀드·ETF·ELB) 템플릿은 라벨 스키마가 없으므로 퇴직연금 일반 스키마로만
  라벨링한다(운용상품 전용 스키마를 만들지 않는다).
- 라벨은 허용 라벨 안에서만, 인용이 그 영역의 최종 text 나 기존 표 셀에 통째로 있을 때만 남긴다. 인용이 맞지 않는 라벨은
  버리고 trace 에 남긴다(재시도·규칙 추가 없음). 맞는 라벨이 없으면 빈 배열이 정상 결과다.
"""
from __future__ import annotations

import json

from . import semantic_assets as A
from .classify import quote_where, region_items

# label/2(2026-10-27 E1, docs/14): 표 항목명·조건값만으로 라벨을 붙이지 않고, 인용은 구분값의 내용 자체를 보여야 한다는
# 지시를 더하고 영역 목록에서 bbox 를 뺐다(label/1 은 isa01 가입유형 표에 '비과세한도'·'의무가입 기간'을 인용해 라벨을 붙였다).
# label/3(E2): label/2 의 조건 예시(가입대상·한도·기간)가 예금성 스키마에서 라벨을 모두 비우게 해 뺐다(h01·g08 예금 전부 빈 라벨).
# 응답을 영역 별칭 키 객체로 바꾸고 영역당 라벨 수 상한을 둬 같은 라벨·영역 반복(g06)을 막는다.
LABEL_VERSION = "label/3"
MAX_LABELS_PER_REGION = 8
LABEL_MAX_REGIONS = 20
LABEL_MAX_CHARS = 8000

LABEL_RULES = (
    "당신은 금융 광고 문구의 구분값 라벨러입니다. 각 영역의 문구가 어느 구분값의 문구인지 정합니다. 법규 준수·적정성은 "
    "판단하지 않습니다.\n"
    "[지시]\n"
    "- 각 영역의 text(표는 rows 포함)에 실제로 들어 있는 구분값을 허용 라벨에서 빠짐없이 모두 고릅니다. 한 영역에 서로 다른 "
    "구분값이 함께 있으면 모두 씁니다.\n"
    "- 맞는 라벨이 없으면 [] 입니다. 홍보·혜택 문구, 메뉴·탭, 이벤트 안내, 화면 장식처럼 어느 구분값에도 해당하지 않는 문구와 "
    "허용 라벨에 없는 항목의 문구에는 억지로 붙이지 않습니다.\n"
    "- 라벨마다 quote 에 그 영역의 text 또는 rows 셀에서 그대로 복사한 짧은 인용을 하나 씁니다. 인용은 그 구분값의 내용이 담긴 "
    "문구여야 합니다(다른 항목의 이름·숫자만 인용하지 않음). 같은 라벨을 한 영역에 두 번 쓰지 않습니다. 다른 영역의 문구나 "
    "라벨 지식의 예시 문구를 인용하지 않습니다.\n"
    "- 원문 표기가 틀렸거나 기준과 달라도 그 구분값의 내용이면 라벨을 붙입니다.\n"
    "- 상품 소속은 이미 정해졌습니다. 다시 판단하지 않습니다. scope=mixed 영역에는 다른 상품의 내용도 함께 있으니 이 상품에 "
    "해당하는 내용에만 라벨을 붙입니다(this_product 는 이 상품 부분의 인용).\n"
)


def label_keys(product: dict) -> list[str]:
    """상품의 라벨 스키마(운용상품 템플릿 제외). 분류되지 않은 상품은 없음."""
    if product["status"] != "classified":
        return []
    return list(dict.fromkeys(A.template(c)["label_schema"] for c in product["codes"]))


def plan_jobs(b: dict, products: list[dict], assignments: dict) -> tuple[list[dict], dict]:
    """상품별 라벨링 작업(영역 묶음). 반환: (작업 목록, 호출 없이 정하는 결과 {(region_id, product_id): 사유})."""
    jobs, direct = [], {}
    for p in products:
        keys = label_keys(p)
        aliases = [a for a in b["regions"] if p["product_id"] in assignments[b["regions"][a]["region_id"]]["product_ids"]
                   and assignments[b["regions"][a]["region_id"]]["status"] == "completed"]
        todo = []
        for a in aliases:
            r = b["regions"][a]
            if not keys:
                direct[(r["region_id"], p["product_id"])] = "product_not_classified"
            elif r["text"] is None:
                direct[(r["region_id"], p["product_id"])] = "no_final_text"
            elif r["text"].strip() == "" and not r["table"]:
                direct[(r["region_id"], p["product_id"])] = "empty_text"      # 글자 없음 영역: 라벨 없음으로 확정(호출 없음)
            else:
                todo.append(a)
        chunk, size = [], 0
        for a in todo:
            n = len(b["regions"][a]["text"]) + len(json.dumps((b["regions"][a]["table"] or {}).get("rows") or [], ensure_ascii=False))
            if chunk and (len(chunk) >= LABEL_MAX_REGIONS or size + n > LABEL_MAX_CHARS):
                jobs.append({"product": p, "keys": keys, "regions": chunk, "index": len([j for j in jobs if j["product"] is p])})
                chunk, size = [], 0
            chunk.append(a)
            size += n
        if chunk:
            jobs.append({"product": p, "keys": keys, "regions": chunk, "index": len([j for j in jobs if j["product"] is p])})
    return jobs, direct


def label_schema(region_ids: list[str], labels: list[str]) -> dict:
    """응답: {"regions": {영역 별칭: [{label, quote}…]}}. 영역마다 정확히 한 번(키), 영역당 라벨 수 상한."""
    lab = {"type": "object", "additionalProperties": False, "required": ["label", "quote"],
           "properties": {"label": {"type": "string", "enum": labels}, "quote": {"type": "string"}}}
    per = {"type": "array", "maxItems": min(len(labels), MAX_LABELS_PER_REGION), "items": lab}
    return {"type": "object", "additionalProperties": False, "required": ["regions"],
            "properties": {"regions": {"type": "object", "additionalProperties": False, "required": list(region_ids),
                                       "properties": {a: per for a in region_ids}}}}


def label_request(b: dict, job: dict, assignments: dict, products: dict[str, dict]) -> dict:
    p = job["product"]
    labels, know = A.knowledge(job["keys"])
    extra = {}
    for a in job["regions"]:
        x = assignments[b["regions"][a]["region_id"]]
        e = {"scope": x["scope"]}
        if x["scope"] == "mixed":
            others = [products[o]["name"] or "이름 없는 상품" for o in x["product_ids"] if o != p["product_id"]]
            mine = [q["quote"] for q in x["parts"] if q["product_id"] == p["product_id"] and q["valid"]]
            e |= {"other_products": others, "this_product": mine}
        extra[a] = e
    tids = [A.template(c)["template_id"] for c in p["codes"]]
    text = (LABEL_RULES + f"[허용 라벨] {', '.join(labels)}\n[라벨 지식]\n{know}\n"
            + f"[대상 상품] 이름: {p['name'] or '(광고에 이름 없음)'} · 템플릿: {', '.join(tids)}\n"
            + f"[영역 목록] 아래 영역 {len(job['regions'])}개를 모두 regions 에 한 번씩 씁니다.\n"
            + region_items(b, job["regions"], extra, bbox=False)
            + "\n[다시 확인] regions 에 영역 ID 마다 라벨 목록을 씁니다. 해당하는 구분값은 빠짐없이 모두(없으면 []), quote 는 그 "
              "영역 문구에서 그대로 복사합니다.")
    return {"version": LABEL_VERSION, "regions": job["regions"], "labels": labels, "text": text, "images": [],
            "schema": label_schema(job["regions"], labels), "max_tokens": min(8000, 600 + 160 * len(job["regions"]))}


def validate_labels(call, req: dict, b: dict) -> dict:
    """라벨 응답 검사: 영역 별칭·허용 라벨·인용 위치. 반환 {ok, errors, regions: {alias: {labels, evidence, rejected}}, missing}."""
    out = {"ok": False, "errors": [], "regions": {}, "missing": []}
    if call.error or not isinstance(call.parsed, dict):
        out["errors"].append(call.error or "응답 없음")
        out["missing"] = list(req["regions"])
        return out
    allowed = set(req["labels"])
    items = call.parsed.get("regions") or {}
    for a in req["regions"]:
        if not isinstance(items, dict) or a not in items:
            continue
        res = {"labels": [], "evidence": [], "rejected": []}
        for lab in items[a] or []:
            name, quote = lab["label"], lab["quote"]
            if name not in allowed:
                res["rejected"].append({"label": name, "quote": quote, "why": "not_allowed"})
                continue
            if name in res["labels"]:
                continue
            where = quote_where(quote, b["regions"][a])
            if where is None:
                res["rejected"].append({"label": name, "quote": quote, "why": "quote_not_in_region"})
                continue
            res["labels"].append(name)
            res["evidence"].append({"label": name, "quote": quote, "where": where})
        # 같은 라벨의 다른 인용이 맞았으면 앞의 인용 실패는 버린 라벨이 아니다
        res["rejected"] = [r for r in res["rejected"] if r["label"] not in res["labels"]]
        out["regions"][a] = res
    out["missing"] = [a for a in req["regions"] if a not in out["regions"]]
    out["ok"] = True
    return out
