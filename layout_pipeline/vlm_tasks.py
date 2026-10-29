"""VLM 판독 작업: 일반 영역(A: 원래 페이지 배치, B: 검출 영역 전체 crop)과 OCR 표 후보 확인.

- Region 은 논리 결과 단위다. HTTP 요청 하나에 여러 Region 을 묶는다(같은 페이지 이미지를 영역 수만큼 보내지 않음).
- 줄 시트(OCR 줄 crop 재배치)는 만들지 않는다. 번호 표시는 원문을 가리지 않도록 별도 사본에만 그린다.
- VLM 은 bbox/Region 을 만들거나 고치지 않는다. 결과 ID 는 요청 ID 집합과 정확히 같아야 한다.
- 미배정 OCR 은 Region 초안이 아니다. 영역 경계에 걸친 OCR 은 '배정되지 않은 참고'로 따로 표시해 전달한다.
"""
from __future__ import annotations

import json
import math
import os
import re

from PIL import Image

from .text_eval import digit_groups, norm, semiglobal, similarity_cover

MISMATCH_MIN_COVER = 0.85   # OCR↔VLM 상호 덮임 비율 하한(잠정). 미만이면 ocr_vlm_mismatch
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|<>]{1,40}\|>")
# 응답 구조 잔여물이 문구에 섞인 흔적(기준선·v3 표 후보에서 관측: '"}<be_thoughtful>{', '"}lot}ᄂ]{', '```json').
OUTPUT_ARTIFACT_RE = re.compile(r'"\}|\{"|```|<[A-Za-z_]{3,40}>|</?(?:br|p|td|tr|th|table|div|span)\s*/?>', re.I)
# 영역 상자 번호(R01·L003 같은 ASCII)를 그리는 글꼴. Windows 는 이전 결과와 같은 이미지가 나오게 맑은 고딕을 먼저 쓴다.
# Linux 컨테이너는 LP_FONT_PATH(모듈을 불러올 때 읽으므로 .env 가 아니라 실제 환경 변수) 또는 설치된 Noto CJK·DejaVu.
FONT_CANDIDATES = ("C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _font_path() -> str:
    configured = os.environ.get("LP_FONT_PATH", "").strip()
    if configured:
        return configured
    return next((p for p in FONT_CANDIDATES if os.path.isfile(p)), FONT_CANDIDATES[0])


FONT = _font_path()
COLORS =[(214, 39, 40), (31, 119, 180), (44, 160, 44), (148, 103, 189), (255, 127, 14), (23, 190, 207),
          (188, 189, 34), (227, 119, 194), (140, 86, 75)]


def crop_box(bbox, width, height) -> list[int]:
    return [max(0, math.floor(bbox[0])), max(0, math.floor(bbox[1])),
            min(width, math.ceil(bbox[2])), min(height, math.ceil(bbox[3]))]


def _ocr_order(ocr_id: str) -> tuple:
    view, _, idx = ocr_id.rpartition("-T")
    return (view, int(idx) if idx.isdigit() else 0)


def region_context(record: dict, region: dict) -> dict:
    """영역의 OCR 참고 문구. 배정 OCR 과 경계에 걸친 미배정 OCR 을 원래 OCR 순서로 함께 보존하고 출처를 표시한다.

    배정 여부로 순서를 바꾸지 않는다(배정 목록 뒤에 경계 목록을 이어 붙이면 비교용 순서가 왜곡된다)."""
    cands = []
    for a in record["assignments"]:
        if a["region_id"] == region["region_id"] and a["ocr_text"].strip():
            # 아주 작은 교집합만 있는 줄은 영역에 귀속해 보존하되 VLM 에 확정 OCR 초안으로 주지 않는다.
            weak = "low_ocr_overlap" in a.get("assignment_review", [])
            cands.append({"ocr_id": a["ocr_id"], "text": a["ocr_text"],
                          "source": "boundary_assigned" if weak else "assigned",
                          "inside_ratio": a.get("inside_ratio", 1.0), "bbox": a.get("ocr_bbox")})
    for u in record["unassigned_texts"]:
        if not u["ocr_text"].strip():
            continue
        for c in u["candidates"]:
            if c["region_id"] == region["region_id"]:
                cands.append({"ocr_id": u["ocr_id"], "text": u["ocr_text"], "source": "boundary_unassigned",
                              "inside_ratio": c["inside_ratio"], "reason": u["reason"], "bbox": u.get("ocr_bbox")})
    cands.sort(key=lambda c: _ocr_order(c["ocr_id"]))
    return {"ocr_candidates": cands,
            "ocr_draft": [c["text"] for c in cands if c["source"] == "assigned"],
            "boundary_ocr": [c for c in cands if c["source"] != "assigned"]}


def legacy_context(record: dict, region: dict) -> dict:
    """crop/1·page/1·table-check/3 요청에 넣던 형식 그대로(기준선 재현용): 배정 초안 목록 + 경계 OCR 목록."""
    ctx = region_context(record, region)
    return {"ocr_draft": ctx["ocr_draft"],
            "boundary_ocr": [{"ocr_id": b["ocr_id"], "text": b["text"], "inside_ratio": b["inside_ratio"], "reason": b["reason"]}
                             for b in ctx["boundary_ocr"]]}


BOUNDARY_CANDIDATE_MIN_INSIDE = 0.5   # crop/3·table-check/5: 이보다 적게 걸친 경계 OCR 은 후보로 주지 않는다


def candidate_context(record: dict, region: dict, version: str = "crop/2") -> dict:
    """crop/2·3, table-check/4·5 의 OCR 참고 표현: 후보 목록과 출처. 목록 순서는 CANDIDATE_ORDER 로 정한다.

    text(기본): 문자열 순 정렬. OCR 배열 순서가 읽기 순서로 쓰이지 않도록 원래 OCR 순서를 일부러 감춘다.
    position: 위치 순(위→아래, 같은 줄은 왼쪽→오른쪽). 문자열 순 목록을 모델이 그대로 따라 쓴 사례(docs/18: tp02 S004 줄 섞임,
    tp06 L002 '(' 로 시작하는 줄이 맨 앞에 중복) 때문에 비교한다. 같은 문구가 여러 번 검출되면 그대로 둔다.
    v3(crop/3·table-check/5): 경계 OCR 은 영역 안 비율 0.5 이상만 주고 그 비율을 함께 적는다. crop/2 에서 영역 안 비율
    0.002 인 경계 OCR(f01 L008 'NH더퍼스트기업대출')을 그림만 있는 crop 의 문구로 옮긴 사례 때문이다."""
    ctx = region_context(record, region)
    v3 = version in ("crop/3", "table-check/5")
    pairs = []
    for c in ctx["ocr_candidates"]:
        if c["source"] == "assigned":
            pairs.append((c, {"text": c["text"], "source": "in_region"}))
        elif not v3:
            pairs.append((c, {"text": c["text"], "source": "boundary"}))
        elif c["inside_ratio"] >= BOUNDARY_CANDIDATE_MIN_INSIDE:
            pairs.append((c, {"text": c["text"], "source": "boundary", "inside_ratio": round(c["inside_ratio"], 2)}))
    if CANDIDATE_ORDER == "position":
        rank = {id(c): k for k, c in enumerate(position_order([c for c, _ in pairs]))}
        return {"ocr_candidates": [item for c, item in sorted(pairs, key=lambda p: rank[id(p[0])])]}
    return {"ocr_candidates": sorted((item for _, item in pairs), key=lambda c: (c["text"], c["source"]))}


SAME_LINE_OVERLAP = 0.5   # 세로 겹침이 작은 상자 높이의 이 비율 이상이면 같은 줄


def position_order(cands: list[dict]) -> list[dict]:
    """OCR 상자를 위→아래 줄로 묶고 줄 안은 왼쪽→오른쪽으로 놓는다(위치만 쓰고 의미로 재배열하지 않는다)."""
    lines: list[list[dict]] = []
    for c in sorted((c for c in cands if c.get("bbox")), key=lambda c: (c["bbox"][1], c["bbox"][0])):
        b = c["bbox"]
        for line in lines:
            ref = line[0]["bbox"]
            overlap = min(b[3], ref[3]) - max(b[1], ref[1])
            if overlap >= SAME_LINE_OVERLAP * min(b[3] - b[1], ref[3] - ref[1]):
                line.append(c)
                break
        else:
            lines.append([c])
    return [c for line in lines for c in sorted(line, key=lambda c: c["bbox"][0])] + [c for c in cands if not c.get("bbox")]


# 후보 목록 순서 설정(run_pipeline --ocr-candidate-order). 프롬프트의 순서 설명 문구도 이 값으로 바꾼다(order_prompt).
CANDIDATE_ORDER = "text"
CANDIDATE_ORDER_VERSIONS = {"text": "text-sorted/1", "position": "position/1"}
_ORDER_WORDING = [
    ("OCR 후보 목록의 순서는 읽기 순서가 아닙니다(문자열 순으로 정렬해 둔 목록입니다).",
     "OCR 후보 목록은 위치 순(위→아래, 같은 줄은 왼쪽→오른쪽)으로 나열했습니다. 단·표·도해·각주에서는 읽기 순서와 다를 수 있으니 순서는 이미지로 정합니다."),
    ("OCR 후보 목록의 순서는 읽기 순서가 아닙니다.",
     "OCR 후보 목록은 위치 순(위→아래, 같은 줄은 왼쪽→오른쪽)이며 단·표·도해·각주에서는 읽기 순서와 다를 수 있습니다."),
    ("(문자열 순 정렬, 틀릴 수 있음)", "(위치 순 나열, 틀릴 수 있음)"),
    ("(순서 없음, 틀릴 수 있음)", "(위치 순 나열, 읽기 순서와 다를 수 있음, 틀릴 수 있음)"),
]


def order_prompt(text: str) -> str:
    """CANDIDATE_ORDER=position 이면 프롬프트의 '문자열 순 정렬' 설명을 위치 순 설명으로 바꾼다(text 이면 그대로)."""
    if CANDIDATE_ORDER != "position":
        return text
    for old, new in _ORDER_WORDING:
        text = text.replace(old, new)
    return text


def short(region_id: str) -> str:
    return region_id.rsplit("-", 1)[-1]


RULES = ("규칙: 상자(영역) 밖 글자는 넣지 마세요. 위→아래, 왼→오른쪽 순서로 쓰고 줄이 바뀌는 곳은 줄바꿈하세요. "
         "요약·번역·맞춤법 교정·숫자 보충을 하지 마세요. 손글씨·로고·그림 속 글자도 보이면 전사하세요. "
         "글자가 전혀 없으면 빈 문자열, 읽을 수 없으면 null 로 두세요. 확신이 없으면 uncertain=true. "
         "OCR 초안과 경계 OCR 은 오류가 있을 수 있는 참고 자료입니다. 이미지와 다르면 이미지를 따르세요. "
         "경계 OCR 은 이 영역에 일부만 걸친 글자이므로 상자 안에 보이는 부분만 전사하세요.")


# 판독 원칙 v2(crop/2·table-check/4): 최종 문구와 읽기 순서는 이미지로 정하고 OCR 은 글자 후보·누락 점검용 참고로만 준다.
GENERAL_REQUEST_VERSIONS = ("crop/1", "crop/2")
ORDER_RULES_V2 = (
    "[읽기 순서] 이미지의 시각적 배치와 문구 사이의 관계로 정합니다. OCR 후보 목록의 순서는 읽기 순서가 아닙니다"
    "(문자열 순으로 정렬해 둔 목록입니다).\n"
    "- 제목 다음에 그 설명을 쓰고, 본문은 위에서 아래로 씁니다.\n"
    "- 좌우로 나뉜 비교·시나리오·단(열)은 한 블록을 끝까지 쓴 뒤 다음 블록을 씁니다. 서로 다른 블록의 줄을 한 줄로 섞지 마세요.\n"
    "- 항목(라벨)과 값이 좌우 또는 위아래로 짝지어 있으면 항목 바로 뒤에 그 값을 씁니다. 항목이나 값이 여러 줄이면 그 항목의 줄을 "
    "모두 쓴 뒤 값의 줄을 씁니다. 라벨이 한 줄로 늘어서고 그 아래에 값이 있으면 라벨마다 그 아래 값을 짝지어 씁니다.\n"
    "- 도표·도해 안의 문구는 시각적으로 이어진 요소끼리(예: 구분 이름 → 조건 → 수치) 묶어서 씁니다.\n"
    "- 각주·주석은 이미지에서 놓인 위치대로 씁니다.\n"
    "- 줄이 바뀌는 곳은 줄바꿈합니다. 같은 위치의 문구를 두 번 쓰지 마세요. 원본에 실제로 여러 번 나오는 문구는 나온 횟수대로 씁니다.\n")
TEXT_RULES_V2 = (
    "[글자] 이미지에 보이는 글자가 기준입니다.\n"
    "- 로고·머리글·배지·버튼·손글씨·그림 속 글자도 보이면 모두 전사합니다.\n"
    "- OCR 후보는 같은 영역을 OCR 이 읽은 참고 후보이며 틀릴 수 있습니다. 후보 글자가 이미지와 다르면(비슷한 모양의 다른 글자로 "
    "읽은 OCR 오독 등) 이미지대로 고쳐 씁니다. OCR 후보를 그대로 따르라는 뜻이 아닙니다. 후보에 없어도 이미지에 보이는 글자는 "
    "쓰고, 후보에 있어도 이미지에 보이지 않으면 쓰지 않습니다.\n"
    "- 고쳐 쓰는 것은 OCR 이 잘못 읽은 글자를 이미지대로 되돌리는 것뿐입니다. 이미지에 실제로 쓰인 표기는 맞춤법·문법·조사가 "
    "어색해 보여도 바꾸지 않습니다(원본의 오타·특이 표현 보존). 문장을 다듬거나 다른 단어로 바꾸지 마세요.\n"
    "- 금액·금리·기간·날짜·상품명은 이미지에 보이는 그대로 씁니다. 알고 있는 상품 지식이나 흔한 값으로 채우거나 바꾸지 마세요.\n"
    "- 이미지에 없는 제목·설명·연결어·인과관계를 만들지 말고, 요약·번역·보충하지 마세요.\n"
    "[불확실] 글자를 확신할 수 없거나 어느 블록·항목에 속하는지 연결이 불명확하면 uncertain=true. 글자가 전혀 없으면 빈 문자열, "
    "판독할 수 없으면 null.\n"
    "OCR 후보 source: in_region = 영역 안의 OCR 글자, boundary = 영역 가장자리에 걸친 OCR 글자(이미지에 보이는 부분만 전사).\n")


# v3 추가 지시(crop/2 비교에서 확인한 회귀 원인: 후보 나열 반복, 그림을 읽은 OCR 후보 전사, 겹친 영역 문구 생략)
EXTRA_RULES_V3 = (
    "[주의] OCR 후보는 확인용입니다. 후보 목록을 옮겨 적거나 나열하지 마세요. 그림·아이콘·무늬를 OCR 이 글자로 읽은 "
    "후보도 있으니, 이미지에서 글자로 보이지 않으면 쓰지 않습니다. 각 이미지는 따로 전사합니다. 다른 이미지(다른 영역)에 "
    "같은 문구가 보여도 이 이미지 안에 보이면 이 영역에 씁니다. '두 번 쓰지 말라'는 것은 한 이미지 안에서 같은 위치를 "
    "반복하지 말라는 뜻입니다. boundary 후보의 inside_ratio 는 그 OCR 글자 상자가 이 영역 안에 들어온 비율입니다.\n")
GENERAL_REQUEST_VERSIONS = GENERAL_REQUEST_VERSIONS + ("crop/3",)


def read_schema(ids: list[str]) -> dict:
    item = {"type": "object", "additionalProperties": False, "required": ["region_id", "text", "uncertain"],
            "properties": {"region_id": {"type": "string", "enum": ids}, "text": {"type": ["string", "null"]},
                           "uncertain": {"type": "boolean"}}}
    return {"type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": item, "minItems": len(ids), "maxItems": len(ids)}}}


# v1: rows=[{cells:[..]}], text 다음 rows → is_table=true 18건 모두 rows 빈 배열.
# v2: 같은 객체 형태로 rows 를 text 앞에 → 여전히 18건 모두 빈 배열.
# 진단(work/vlm/diag-table-rows): 같은 요청에서 rows 를 문자열 배열의 배열로 두면 강제 없이 5행 모두 생성.
# v3: rows = string[][] (rows 를 text 앞에).
# v4: 판독 원칙 v2(이미지 기준 읽기 순서·정렬된 OCR 후보·원문 표기 보존). 스키마는 v3 와 같다.
TABLE_REQUEST_VERSION = "table-check/3"
TABLE_REQUEST_VERSIONS = ("table-check/3", "table-check/4", "table-check/5")


def table_schema(region_id: str) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["region_id", "is_table", "rows", "text", "uncertain"],
            "properties": {"region_id": {"type": "string", "enum": [region_id]}, "is_table": {"type": "boolean"},
                           "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                           "text": {"type": ["string", "null"]}, "uncertain": {"type": "boolean"}}}


def row_cells(row) -> list[str]:
    """v3(string[]) 와 v1/v2({cells: string[]}) 응답을 모두 읽는다(저장 결과 재평가용)."""
    return row.get("cells", []) if isinstance(row, dict) else list(row)


MARGIN_RULE = {"name": "ocr-median-0.3/1",
               "statement": "여백 = max(4, round(0.3 × 페이지의 글자 있는 OCR 상자 높이 중앙값)) px, 네 변 동일, 페이지 경계에서 자름. "
                            "실행 전에 고정. 저장 bbox 는 바꾸지 않고 모델 입력 crop 에만 적용"}


def page_margin(record: dict) -> int:
    hs = sorted(x["ocr_bbox"][3] - x["ocr_bbox"][1] for x in record["assignments"] + record["unassigned_texts"]
                if x["ocr_text"].strip())
    if not hs:
        return 4
    mid = hs[len(hs) // 2] if len(hs) % 2 else (hs[len(hs) // 2 - 1] + hs[len(hs) // 2]) / 2
    return max(4, round(0.3 * mid))


def crop_request(record: dict, regions: list[dict], page_img: Image.Image, margin_px: int = 0, version: str = "crop/1") -> dict:
    """B: 검출 영역 전체를 원본에서 자른 이미지(이미지별 ID). 저장 bbox 는 바꾸지 않는다.

    margin_px > 0 이면 crop 범위만 넓히고(문맥 확보용), 대상 영역의 crop 내 위치를 함께 알린다.
    여백에만 있는 이웃 글자는 대상 영역 문구가 아니다."""
    images, items = [], []
    for k, r in enumerate(regions, 1):
        target = crop_box(r["bbox"], page_img.width, page_img.height)
        box = crop_box([r["bbox"][0] - margin_px, r["bbox"][1] - margin_px, r["bbox"][2] + margin_px,
                        r["bbox"][3] + margin_px], page_img.width, page_img.height) if margin_px else target
        images.append(page_img.crop(box))
        item = {"image_index": k, "region_id": r["region_id"], "crop_px": box}
        if margin_px:
            item["target_in_crop_px"] = [target[0] - box[0], target[1] - box[1], target[2] - box[0], target[3] - box[1]]
        items.append(item | (candidate_context(record, r, version) if version in ("crop/2", "crop/3")
                             else legacy_context(record, r)))
    if version in ("crop/2", "crop/3"):
        if margin_px:
            raise ValueError(f"{version} 는 여백 없는 영역 crop 에서만 정의된다")
        text = ("첨부 이미지는 순서대로 아래 목록의 검출 영역 전체를 원본 페이지에서 자른 것입니다(이미지 k = image_index k). "
                "각 region_id 에 대해 해당 이미지에 보이는 모든 문구를 원문 그대로 전사하세요. 이미지 가장자리에서 일부가 잘린 글자는 "
                "판독 가능한 범위에서 전사하세요.\n" + ORDER_RULES_V2 + TEXT_RULES_V2
                + (EXTRA_RULES_V3 if version == "crop/3" else "")
                + "영역 목록:\n" + json.dumps(items, ensure_ascii=False))
        return {"mode": "crop", "version": version, "margin_px": 0, "region_ids": [r["region_id"] for r in regions],
                "images": images, "image_roles": [f"region_crop:{r['region_id']}" for r in regions], "text": text,
                "schema": read_schema([r["region_id"] for r in regions]), "items": items}
    if margin_px:
        head = ("첨부 이미지는 순서대로 아래 목록의 검출 영역 주변에 여백을 조금 더해 원본 페이지에서 자른 것입니다(이미지 k = image_index k). "
                "각 이미지에서 대상 영역은 target_in_crop_px(이미지 좌표 x0,y0,x1,y1) 범위이고, 그 바깥 여백은 글자 모양 확인을 위한 "
                "문맥입니다. 각 region_id 에 대해 대상 영역 안에 있거나 대상 영역 경계에 걸친 글자를 원문 그대로 전사하고, 여백에만 있는 "
                "다른 글자는 전사하지 마세요. 확신이 없으면 uncertain=true. ")
    else:
        head = ("첨부 이미지는 순서대로 아래 목록의 검출 영역 전체를 원본 페이지에서 자른 것입니다(이미지 k = image_index k). "
                "각 region_id 에 대해 해당 이미지에 보이는 모든 글자를 원문 그대로 전사하세요. 이미지 가장자리에서 일부가 잘린 글자는 "
                "판독 가능한 범위에서 전사하고 확신이 없으면 uncertain=true. ")
    text = head + RULES + "\n영역 목록:\n" + json.dumps(items, ensure_ascii=False)
    return {"mode": "crop", "version": "crop-margin/1" if margin_px else "crop/1", "margin_px": margin_px,
            "region_ids": [r["region_id"] for r in regions], "images": images,
            "image_roles": [f"region_crop:{r['region_id']}" for r in regions], "text": text,
            "schema": read_schema([r["region_id"] for r in regions]), "items": items}


def table_request(record: dict, region: dict, page_img: Image.Image, version: str = TABLE_REQUEST_VERSION) -> dict:
    box = crop_box(region["bbox"], page_img.width, page_img.height)
    if version in ("table-check/4", "table-check/5"):
        ctx = candidate_context(record, region, version)
        v5 = version == "table-check/5"
        text = ("첨부는 OCR 레이아웃이 표 후보로 검출한 영역의 원본 크롭입니다. 이미지를 보고 판단하고 전사하세요.\n"
                "1) is_table: 행과 열, 또는 항목-조건-값이 서로 대응되는 실제 표이면 true. 메뉴·탭·버튼·내비게이션의 나열이나 "
                "단순 문단이면 false. false 여도 text 에 모든 문구를 씁니다.\n"
                "2) rows: is_table=true 이면 표의 모든 행을 위에서 아래로 빠짐없이 채웁니다(머리글 행, 여러 줄 설명이 있는 행, "
                "표 안의 주석·비고 행 포함). 각 행은 왼쪽→오른쪽 셀 문자열 배열입니다. 한 셀 안의 여러 줄은 공백으로 이어 쓰고, "
                "항목·조건과 그 값이 같은 행에 오게 하세요. 여러 행에 걸친 병합 셀은 시작 행에만 씁니다. is_table=false 일 때만 빈 배열.\n"
                "3) text: 표 여부와 관계없이 이미지 안의 모든 문구를 씁니다. 표 부분은 rows 와 같은 행 순서로, 표 밖 문구(제목·각주 등)는 "
                "이미지에서 놓인 위치대로 씁니다.\n"
                + ("text 는 rows 와 내용이 겹쳐도 반드시 모든 문구를 씁니다. 글자가 있는데 text 를 빈 문자열로 두지 마세요.\n" if v5 else "")
                + ORDER_RULES_V2 + TEXT_RULES_V2 + (EXTRA_RULES_V3 if v5 else "")
                + json.dumps({"region_id": region["region_id"], "crop_px": box, **ctx}, ensure_ascii=False))
        return {"mode": "table", "version": version, "region_ids": [region["region_id"]], "images": [page_img.crop(box)],
                "image_roles": [f"table_candidate_crop:{region['region_id']}"], "text": text,
                "schema": table_schema(region["region_id"]), "items": [{"region_id": region["region_id"], "crop_px": box, **ctx}]}
    ctx = legacy_context(record, region)
    text = ("첨부는 OCR 레이아웃이 표 후보로 검출한 영역의 원본 크롭입니다.\n"
            "1) is_table: 행과 열, 또는 항목-조건-값이 서로 대응되는 실제 표이면 true. 메뉴·탭·버튼·내비게이션의 나열이나 "
            "단순 문단이면 false.\n"
            "2) rows: is_table=true 이면 반드시 표의 모든 행을 채우세요. 각 행은 왼쪽→오른쪽 셀 문자열 배열입니다. 한 셀 안의 "
            "여러 줄은 공백으로 이어 쓰고, 항목·조건과 그 값이 같은 행에 오게 하세요. 여러 행에 걸친 병합 셀은 시작 행에만 "
            "쓰세요. is_table=false 일 때만 빈 배열.\n"
            "3) text: 표 여부와 관계없이 이미지 안의 모든 글자를 원문 그대로, 위→아래·왼→오른쪽 순서로 줄바꿈해 쓰세요.\n"
            "요약·교정·숫자 보충 금지. 읽을 수 없으면 text=null, 확신이 없으면 uncertain=true. "
            "OCR 초안과 경계 OCR 은 오류가 있을 수 있는 참고 자료입니다.\n"
            + json.dumps({"region_id": region["region_id"], "crop_px": box, **ctx}, ensure_ascii=False))
    return {"mode": "table", "version": TABLE_REQUEST_VERSION, "region_ids": [region["region_id"]], "images": [page_img.crop(box)],
            "image_roles": [f"table_candidate_crop:{region['region_id']}"], "text": text,
            "schema": table_schema(region["region_id"]), "items": [{"region_id": region["region_id"], "crop_px": box, **ctx}]}


def validate(call, request: dict) -> dict[str, dict]:
    """응답 계약 검사. region_id → {값, contract_errors}. bool() 강제 변환을 하지 않는다."""
    ids = request["region_ids"]
    out = {i: {"region_id": i, "text": None, "uncertain": None, "contract_errors": []} for i in ids}
    errors = []
    if call.error:
        errors.append(call.error)
    parsed = call.parsed
    if request["mode"] == "table":
        results = [parsed] if isinstance(parsed, dict) else []
    else:
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(results, list):
            errors.append("results 배열 없음")
            results = []
    seen = [r.get("region_id") for r in results if isinstance(r, dict)]
    if sorted(seen) != sorted(ids):
        errors.append(f"region_id 집합 불일치: 누락 {sorted(set(ids) - set(seen))}, 추가 {sorted(set(seen) - set(ids))}, "
                      f"중복 {sorted({x for x in seen if seen.count(x) > 1})}")
    for r in results:
        if not isinstance(r, dict) or r.get("region_id") not in out:
            continue
        cur = out[r["region_id"]]
        errs = cur["contract_errors"]
        text = r.get("text")
        if text is not None and not isinstance(text, str):
            errs.append(f"text 타입 {type(text).__name__}")
            text = None
        if not isinstance(r.get("uncertain"), bool):
            errs.append(f"uncertain 타입 {type(r.get('uncertain')).__name__}")
        cur.update(text=text, uncertain=r.get("uncertain") if isinstance(r.get("uncertain"), bool) else None)
        if request["mode"] == "table":
            if not isinstance(r.get("is_table"), bool):
                errs.append(f"is_table 타입 {type(r.get('is_table')).__name__}")
            rows = r.get("rows")
            if not isinstance(rows, list) or any(not isinstance(x, (list, dict)) or
                                                 any(not isinstance(c, str) for c in row_cells(x)) for x in rows):
                errs.append("rows 형식 오류")
                rows = None
            else:
                rows = [row_cells(x) for x in rows]
            cur.update(is_table=r.get("is_table") if isinstance(r.get("is_table"), bool) else None, rows=rows)
    for i in ids:
        out[i]["contract_errors"] = errors + out[i]["contract_errors"]
        out[i]["contract_ok"] = not out[i]["contract_errors"]
    return out


def ocr_vlm_agreement(ocr_lines: list[str], text: str) -> tuple[float, float]:
    """OCR↔VLM 상호 일치(읽기 순서 무관).

    - OCR 줄 각각을 VLM 문구 전체의 어느 부분과 맞춘 편집거리 → OCR 글자가 VLM 에 얼마나 있는지.
    - VLM 어절(공백으로 나눈 조각) 각각을 OCR 줄을 원래 순서로 이은 문자열의 어느 부분과 맞춘 편집거리
      → VLM 글자가 OCR 로 얼마나 설명되는지. 어절 단위라서 VLM 이 원본 배치에 맞게 줄 순서를 바꾸거나
      떨어진 항목과 값을 한 줄로 짝지어도(예: '가입대상 개인(적립식)') 불일치로 보지 않는다.
    전체 문자열이나 VLM 줄 단위로 비교하면 이런 정상적인 읽기 순서 재구성을 불일치로 오판한다."""
    lines = [norm(x) for x in ocr_lines if norm(x)]
    tokens = [norm(t) for x in text.splitlines() for t in x.split() if norm(t)]
    out, joined = "".join(tokens), "".join(lines)
    if not lines:
        return 1.0, 1.0 if not out else 0.0
    if not tokens:
        return 0.0, 1.0
    ocr_in_vlm = 1 - sum(min(len(x), semiglobal(x, out)) for x in lines) / sum(len(x) for x in lines)
    vlm_in_ocr = 1 - sum(min(len(x), semiglobal(x, joined)) for x in tokens) / sum(len(x) for x in tokens)
    return ocr_in_vlm, vlm_in_ocr


def review_reasons(record: dict, region: dict, result: dict) -> dict:
    """needs_review 를 여러 독립 근거로 구성한다. uncertain=false 만으로 검토 불필요를 정하지 않는다."""
    ctx = region_context(record, region)
    ref_lines = ctx["ocr_draft"] + [b["text"] for b in ctx["boundary_ocr"] if b["inside_ratio"] >= 0.5]
    reference = "\n".join(ref_lines)
    text = result.get("text")
    quality, context, measures = [], [], {}
    if result.get("contract_errors"):
        quality.append("contract_error")
    if text is None:
        quality.append("read_failed")
    elif SPECIAL_TOKEN_RE.search(text):
        # JSON 스키마를 통과해도 모델 내부 토큰이 문구에 섞일 수 있다(실험에서 f12_p02 표 후보). 문구는 고치지 않는다.
        quality.append("special_token_in_text")
    if text is not None and OUTPUT_ARTIFACT_RE.search(text):
        # 문구는 고치지 않고 검토 근거로만 남긴다.
        quality.append("output_artifact_in_text")
    if result.get("uncertain") is True:
        quality.append("vlm_uncertain")
    if text is not None:
        if norm(reference) and not norm(text):
            quality.append("empty_but_ocr_has_text")
        elif norm(reference) and norm(text):
            ocr_in_vlm, vlm_in_ocr = ocr_vlm_agreement(ref_lines, text)
            measures.update(ocr_lines_in_vlm=round(ocr_in_vlm, 3), vlm_chars_supported_by_ocr=round(vlm_in_ocr, 3))
            if min(ocr_in_vlm, vlm_in_ocr) < MISMATCH_MIN_COVER:
                quality.append("ocr_vlm_mismatch")
            ocr_digits, vlm_digits = sorted(set(digit_groups(reference))), sorted(set(digit_groups(text)))
            if ocr_digits != vlm_digits:
                quality.append("digit_mismatch")
                measures.update(ocr_digits=ocr_digits, vlm_digits=vlm_digits)
        elif norm(text):
            context.append("no_ocr_reference")
    if result.get("is_table") is False:
        context.append("table_candidate_rejected")
    if result.get("is_table") is True and not result.get("rows"):
        quality.append("table_rows_missing")
    elif result.get("is_table") is True and text:
        # rows 가 text 의 글자를 얼마나 덮는지(행 누락 표시용, 결과를 고치지 않음)
        rows_text = "\n".join(" ".join(row_cells(r)) for r in result["rows"])
        cover = similarity_cover(text, rows_text)
        measures["rows_cover_text"] = round(cover, 3)
        if cover < MISMATCH_MIN_COVER:
            quality.append("table_rows_incomplete")
    if any(region["region_id"] in (o["a"], o["b"]) for o in record["diagnostics"]["region_overlaps"]):
        context.append("region_overlap")
    if region["crossing_ocr_ids"] or region["ambiguous_ocr_ids"]:
        context.append("ocr_crosses_region_boundary")
    return {"needs_review": bool(quality or context), "quality_reasons": quality, "context_reasons": context,
            "measures": measures, "ocr_reference": reference,
            "rule": {"mismatch_min_cover": MISMATCH_MIN_COVER, "status": "provisional"}}
