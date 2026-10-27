"""표 판독 table-cells/1(docs/11 §2): VLM 이 표 후보 하나의 문구(text)와 논리 셀 구조(cells)를 같은 응답으로 읽는다.

- 입력: 원본 표 영역 crop(표시 없음) + 그 영역이 속한 판독 단위(페이지·타일·띠) 문맥 이미지(대상 밖 어둡게, 바깥 테두리).
- OCR 후보는 crop/3 표현(문자열 순 정렬, 출처 표시)이며 셀 구분·행 순서의 근거가 아니다.
- 셀: row·col(0부터 논리 위치), rowspan·colspan(≥1), text. 병합 셀 문구는 시작 셀에 한 번. 격자의 모든 위치는 정확히 한 셀이
  덮는다(실제 빈 칸은 text "" 셀). 계약 검사를 통과한 응답의 text·cells 를 하나의 후보로 쓰고, rows 는 cells 에서 변환한 보기다.
- 영역 bbox 는 표 전체 bbox 하나 그대로. 셀 bbox·HTML 복원·스타일 정보는 만들지 않는다.
"""
from __future__ import annotations

import json

from PIL import Image

from . import page_read, vlm_tasks
from .text_eval import similarity_cover

# 지시 1회 수정 시도 /2(docs/11 R3: 병합 판단·text 범위·줄바꿈 지시 추가)는 계약 통과 9/12 로 /1(11/12)보다 나빠 기각했다.
# /1 의 실패 유형(문맥 이미지에서만 보이는 문구·태그 유입, 위치 겹침)은 런타임 검사(상자 밖 문구·출력 잔여물·셀 계약)로 표시한다.
VERSION = "table-cells/1"
MAX_TOKENS = 6000
CELLS_COVER_MIN = 0.85     # 셀 문구가 text 의 글자를 덮는 비율 하한(기존 rows_cover_text 와 같은 값). 미만이면 품질 검토

RULES = (
    "할 일: 대상 영역의 표 여부와 문구, 표의 논리 셀 구조를 원본에서 직접 읽어 하나의 결과로 씁니다.\n"
    "1) is_table: 행과 열(또는 항목-조건-값)이 서로 대응하는 실제 표이면 true. 메뉴·탭·버튼·주소창·내비게이션 나열이나 단순 "
    "문단이면 false.\n"
    "2) cells: is_table=true 이면 표를 n_rows 행 × n_cols 열의 논리 격자로 보고 셀 목록을 씁니다.\n"
    "- row, col 은 0부터 시작하는 논리 위치입니다(픽셀 좌표가 아님). 머리글 행도 행입니다.\n"
    "- 선·배경색·글자 정렬을 보고 각 칸이 차지하는 범위를 판단합니다. 여러 행에 걸친 칸은 rowspan, 여러 열에 걸친 칸은 colspan 으로 "
    "쓰고 문구는 그 칸의 시작 셀(가장 위·가장 왼쪽)에 한 번만 씁니다. 병합 칸의 문구를 다른 행·열 셀에 반복해 쓰지 않습니다.\n"
    "- 격자의 모든 위치는 정확히 한 셀이 덮어야 합니다. 글자가 없는 실제 빈 칸은 text 가 빈 문자열인 셀로 씁니다.\n"
    "- 각 셀에는 그 칸 안에 보이는 문구만 씁니다. 다른 칸의 문구를 옮기거나 섞지 않습니다. 칸 안의 여러 줄은 공백 하나로 이어 씁니다.\n"
    "- 표 밖의 제목·캡션·각주는 셀로 만들지 않고 text 에만 씁니다.\n"
    "- is_table=false 이면 n_rows=0, n_cols=0, cells=[] 입니다.\n"
    "3) text: 표 여부와 관계없이 대상 영역 안의 모든 문구를 씁니다. 표 부분은 위 행부터, 행 안에서는 왼쪽 칸부터 쓰고 병합 칸 "
    "문구는 한 번만 씁니다. 표 밖 문구는 놓인 위치대로 씁니다. 글자가 있으면 text 를 비우지 않습니다.\n"
    "4) OCR 후보는 같은 영역에서 OCR 이 읽은 글자 조각입니다(문자열 순 정렬, 틀릴 수 있음). 빠진 글자를 확인하는 참고일 뿐 셀 구분·"
    "행 순서의 근거가 아니며, 나열하거나 옮겨 적지 않습니다. 그림·무늬를 글자로 읽은 후보는 이미지에서 글자로 보이지 않으면 쓰지 않습니다.\n"
    "5) 이미지에 보이는 표기를 그대로 씁니다. 맞춤법 교정·요약·번역을 하지 않고 금액·금리·기간을 상품 지식으로 채우거나 바꾸지 않습니다.\n"
    "6) 확신할 수 없으면 uncertain=true 로 두고 notes 에 이유를 씁니다.\n")


def schema(region_id: str) -> dict:
    cell = {"type": "object", "additionalProperties": False, "required": ["row", "col", "rowspan", "colspan", "text"],
            "properties": {"row": {"type": "integer"}, "col": {"type": "integer"}, "rowspan": {"type": "integer"},
                           "colspan": {"type": "integer"}, "text": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["region_id", "is_table", "n_rows", "n_cols", "cells", "text", "uncertain", "notes"],
            "properties": {"region_id": {"type": "string", "enum": [region_id]}, "is_table": {"type": "boolean"},
                           "n_rows": {"type": "integer"}, "n_cols": {"type": "integer"},
                           "cells": {"type": "array", "items": cell}, "text": {"type": ["string", "null"]},
                           "uncertain": {"type": "boolean"}, "notes": {"type": "string"}}}


def context_view_for(record: dict, region: dict) -> dict:
    """영역이 속한 판독 단위: route view(페이지·타일). page-band/1 로 나뉘는 view 면 영역이 든 띠(page-first 와 같은 단위)."""
    view = next(v for v in record["route"]["views"] if v["view_id"] == region["view_id"])
    for bview, bregs in page_read.band_views(view, page_read.view_regions(record, view)):
        if any(r["region_id"] == region["region_id"] for r in bregs):
            return bview
    return view


def request(record: dict, region: dict, page_img: Image.Image, context_view: dict) -> dict:
    box = vlm_tasks.crop_box(region["bbox"], page_img.width, page_img.height)
    cx0, cy0, cx1, cy1 = context_view["crop"]
    tgt = [box[0] - cx0, box[1] - cy0, box[2] - cx0, box[3] - cy0]
    images = [page_img.crop(box), page_read.context_marked(page_img.crop((cx0, cy0, cx1, cy1)), tgt)]
    roles = [f"table_candidate_crop:{region['region_id']}", f"context_marked:{context_view['view_id']}"]
    head = (f"[대상] region_id={region['region_id']}, OCR 레이아웃이 표 후보로 검출한 영역. 원본 좌표 bbox={box}"
            f"(원본 이미지 {page_img.width}x{page_img.height}px).\n"
            "첫 번째 이미지는 이 대상 영역을 원본에서 그대로 자른 crop 입니다(표시 없음). 글자와 표 구조는 이 이미지에서 읽습니다.\n"
            f"두 번째 이미지는 이 영역이 들어 있는 원본 {'띠' if context_view.get('band_of') else '페이지/타일'}"
            f"(원본 좌표 {[cx0, cy0, cx1, cy1]})이며, 대상 영역은 이 이미지 안 {tgt} 입니다. 대상 바깥을 어둡게 하고 바깥에 빨간 "
            "테두리를 그렸습니다. 대상의 위치와 주변 관계를 이해하는 용도이며, 여기서만 보이는 글자는 쓰지 않습니다.\n")
    item = {"region_id": region["region_id"]} | vlm_tasks.candidate_context(record, region, "crop/3")
    return {"mode": "table_cells", "version": VERSION, "region_ids": [region["region_id"]], "images": images,
            "image_roles": roles, "text": head + RULES + "입력:\n" + json.dumps(item, ensure_ascii=False),
            "schema": schema(region["region_id"]), "items": [item], "context_view": context_view}


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def check_cells(is_table, n_rows, n_cols, cells) -> list[str]:
    """기계적 계약 검사: 타입·범위·span·위치 중복 덮임·덮이지 않은 위치."""
    errs = []
    if not isinstance(is_table, bool):
        return ["is_table 타입"]
    if not (_is_int(n_rows) and _is_int(n_cols)) or n_rows < 0 or n_cols < 0:
        return ["n_rows·n_cols 타입·범위"]
    if not isinstance(cells, list):
        return ["cells 타입"]
    if not is_table:
        return [] if not cells and n_rows == 0 and n_cols == 0 else ["is_table=false 인데 cells·격자 있음"]
    if n_rows < 1 or n_cols < 1 or not cells:
        return ["is_table=true 인데 격자·cells 없음"]
    grid = [[0] * n_cols for _ in range(n_rows)]
    for k, c in enumerate(cells):
        if not isinstance(c, dict) or not all(_is_int(c.get(f)) for f in ("row", "col", "rowspan", "colspan")) \
                or not isinstance(c.get("text"), str):
            errs.append(f"cell {k} 형식")
            continue
        r, col, rs, cs = c["row"], c["col"], c["rowspan"], c["colspan"]
        if r < 0 or col < 0 or rs < 1 or cs < 1 or r + rs > n_rows or col + cs > n_cols:
            errs.append(f"cell {k} 범위({r},{col},{rs}x{cs})")
            continue
        for i in range(r, r + rs):
            for j in range(col, col + cs):
                grid[i][j] += 1
    dup = [(i, j) for i in range(n_rows) for j in range(n_cols) if grid[i][j] > 1]
    hole = [(i, j) for i in range(n_rows) for j in range(n_cols) if grid[i][j] == 0]
    if dup:
        errs.append(f"위치 중복 덮임 {dup[:6]}")
    if hole:
        errs.append(f"덮이지 않은 위치 {hole[:6]}")
    return errs


def to_rows(n_rows: int, n_cols: int, cells: list[dict]) -> list[list[str]]:
    """호환 보기: 행마다 열 수만큼 칸, 시작 셀 문구만 쓰고 병합으로 덮인 위치는 ""(셀 구조가 원본 결과)."""
    rows = [[""] * n_cols for _ in range(n_rows)]
    for c in cells:
        rows[c["row"]][c["col"]] = c["text"]
    return rows


def validate(call, req: dict) -> dict[str, dict]:
    rid = req["region_ids"][0]
    out = {"region_id": rid, "text": None, "uncertain": None, "is_table": None, "rows": None, "cells": None,
           "n_rows": None, "n_cols": None, "notes": None, "contract_errors": []}
    if call.error:
        out["contract_errors"].append(call.error)
    p = call.parsed if isinstance(call.parsed, dict) else None
    if p is None:
        out["contract_errors"].append("응답 없음")
    elif p.get("region_id") != rid:
        out["contract_errors"].append(f"region_id 불일치 {p.get('region_id')}")
    else:
        errs = check_cells(p.get("is_table"), p.get("n_rows"), p.get("n_cols"), p.get("cells"))
        text = p.get("text")
        if text is not None and not isinstance(text, str):
            errs.append("text 타입")
        if not isinstance(p.get("uncertain"), bool):
            errs.append("uncertain 타입")
        out["contract_errors"] += errs
        if not errs:
            cells = sorted(p["cells"], key=lambda c: (c["row"], c["col"]))
            out.update(text=text, uncertain=p["uncertain"], is_table=p["is_table"], cells=cells,
                       n_rows=p["n_rows"], n_cols=p["n_cols"], notes=p.get("notes"),
                       rows=to_rows(p["n_rows"], p["n_cols"], cells) if p["is_table"] else [])
    out["contract_ok"] = not out["contract_errors"]
    return {rid: out}


def cells_text(cells: list[dict] | None) -> str:
    return "\n".join(c["text"] for c in cells or [] if c["text"].strip())


def cells_cover(text: str | None, cells: list[dict] | None) -> float | None:
    """셀 문구가 text 의 글자를 덮는 비율(표 밖 캡션·각주가 있으면 1 보다 작을 수 있다)."""
    if not text or not cells:
        return None
    return similarity_cover(text, cells_text(cells))
