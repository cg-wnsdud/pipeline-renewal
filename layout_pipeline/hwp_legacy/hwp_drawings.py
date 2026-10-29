"""HWP 묶음 개체·글상자를 검토 HTML 에 되살린다.

document-processor(DocIR)는 그리기 개체(`hp:container` 묶음 개체, `hp:rect`·`hp:ellipse`
글상자)를 읽지 않는다. 그래서 그 안의 글자는 검토 HTML·쪽 그림·파싱 글자 어디에도 없다.
예금성 광고3(2026-10-16) 1쪽 머리의 "보도자료", "2026년 9월 11일", "자료제공 : 홍보부 …",
"내용문의 : 개인고객부 …"가 모두 한 묶음 개체 안의 글상자라서 통째로 빠졌다.

HWPX 원본(HWP 는 document-processor 변환기로 HWPX 를 다시 만든다)에서 그리기 개체를 읽어
글상자·그림·선·글상자 안 표를 원래 크기와 상대 위치로 다시 놓는다.

- 개체는 쪽 겹으로 한컴이 놓는 용지 좌표에 놓는다. 문단 기준(PARA) 개체의 세로 위치는 저장된
  `vertpos` 가 아니라 개체가 줄을 밀기 전의 문단 위치다(`_natural_tops`). 예금성 광고3 은 이렇게
  놓아야 묶음 안 로고가 1쪽 맨 위 로고(용지 기준 그림)와 겹쳐 한 번만 보인다(한글 화면 확인).
- `자리 차지`(TOP_AND_BOTTOM) 개체는 겹치는 첫 문단 앞에 빈 상자를 두고, 검토 HTML 스크립트가
  그 상자를 개체 아래 끝까지 늘려 뒤 본문을 민다(`.nh-hwp-drawing-spacer`).
- 글상자 안에 떠 있는 표(예금성 광고3 의 결재란)는 한글 화면에 보이지 않으므로 그리지 않는다.
- 글상자 문단은 `<p data-node-id="drawing:…">` 라 쪽 그림에 보이고, 검토 HTML DOM 행으로
  파싱 글자(P1/P3)에도 들어간다(hwp_html 의 `.nh-hwp-textbox` 읽기).

HWP→HWPX 변환기는 글상자 문단에 떠 있는 표가 있으면 표 뒤의 문단 글자를 표 마지막 칸에
붙인다(원본 HWP 레코드로 확인: "보도자료"는 글상자 문단 글자인데 결재란 '국장' 칸에 들어감).
문단에 제 글자가 없고 마지막 칸의 마지막 run 에 글자 조각이 둘 이상이면 뒤 조각을 문단으로
되돌린다. 원본 한컴 화면과 픽셀 단위로 같지는 않다. `HWP_DRAWINGS=off` 로 끈다.
"""
from __future__ import annotations

import base64
import html as html_lib
import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from .hwp_layers import _binary_paths, _box_pt, _local, _MIME, _page_layout, _section_names

_HWPUNIT_PER_PT = 100.0
_SHAPES = {"container", "rect", "ellipse", "polygon", "curve", "arc", "pic", "line"}
_TEXT_SHAPES = {"rect", "ellipse", "polygon", "curve", "arc"}
_ALIGN = {"LEFT": "left", "RIGHT": "right", "CENTER": "center", "JUSTIFY": "justify",
          "DISTRIBUTE": "justify", "DISTRIBUTE_SPACE": "justify"}
_VALIGN = {"TOP": "flex-start", "CENTER": "center", "BOTTOM": "flex-end"}


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local(child.tag) == name), None)


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _num(value: str | None) -> float:
    """HWPUNIT 는 32비트 부호 있는 값을 부호 없이 적는다(4294966512 = -784)."""
    number = float(value or 0)
    if number >= 2 ** 31:
        number -= 2 ** 32
    return number


def _pt(value: str | None) -> float:
    return _num(value) / _HWPUNIT_PER_PT


# ── header.xml: 글자·문단 모양 ─────────────────────────────────────────────

def _styles(archive: zipfile.ZipFile) -> tuple[dict[str, dict], dict[str, str]]:
    try:
        root = ET.fromstring(archive.read("Contents/header.xml"))
    except KeyError:
        return {}, {}
    chars: dict[str, dict] = {}
    aligns: dict[str, str] = {}
    for element in root.iter():
        kind = _local(element.tag)
        if kind == "charPr":
            rel = _child(element, "relSz")
            spacing = _child(element, "spacing")
            size = _num(element.get("height") or "1000") / _HWPUNIT_PER_PT
            if rel is not None and rel.get("hangul"):
                size *= float(rel.get("hangul")) / 100
            chars[str(element.get("id"))] = {
                "size_pt": round(size, 2),
                "color": element.get("textColor") or "#000000",
                "bold": _child(element, "bold") is not None,
                "italic": _child(element, "italic") is not None,
                "spacing_em": float(spacing.get("hangul") or 0) / 100 if spacing is not None else 0.0,
            }
        elif kind == "paraPr":
            align = _child(element, "align")
            aligns[str(element.get("id"))] = _ALIGN.get(
                (align.get("horizontal") if align is not None else "") or "", "left",
            )
    return chars, aligns


# ── 그리기 개체 읽기 ────────────────────────────────────────────────────────

def _matrices(shape: ET.Element) -> tuple[dict[str, float], dict[str, float]]:
    info = _child(shape, "renderingInfo")
    identity = {"e1": 1.0, "e2": 0.0, "e3": 0.0, "e4": 0.0, "e5": 1.0, "e6": 0.0}
    if info is None:
        return identity, identity
    trans = _child(info, "transMatrix")
    scales = _children(info, "scaMatrix")

    def values(element: ET.Element | None) -> dict[str, float]:
        if element is None:
            return dict(identity)
        return {key: float(element.get(key) or identity[key]) for key in identity}

    return values(trans), values(scales[-1] if scales else None)


def _member_box(shape: ET.Element) -> tuple[list[float], bool, bool]:
    """묶음 안 개체 → 묶음 좌상단 기준 [x0, y0, x1, y1] pt, 좌우·상하 뒤집힘."""
    trans, scale = _matrices(shape)
    size = _child(shape, "orgSz")
    width = _num(size.get("width") if size is not None else 0) * scale["e1"]
    height = _num(size.get("height") if size is not None else 0) * scale["e5"]
    x = trans["e3"] + scale["e3"]
    y = trans["e6"] + scale["e6"]
    x0, x1 = sorted((x, x + width))
    y0, y1 = sorted((y, y + height))
    box = [round(value / _HWPUNIT_PER_PT, 2) for value in (x0, y0, x1, y1)]
    return box, width < 0, height < 0


def _runs(paragraph: ET.Element, chars: dict[str, dict]) -> list[dict[str, Any]]:
    runs = []
    for run in _children(paragraph, "run"):
        style = chars.get(str(run.get("charPrIDRef")), {})
        for part in _children(run, "t"):
            text = "".join(part.itertext())
            if text:
                runs.append({"text": text, "style": style})
    return runs


def _table(table: ET.Element, chars: dict[str, dict], aligns: dict[str, str]) -> dict[str, Any]:
    pos = _child(table, "pos")
    size = _child(table, "sz")
    rows = []
    for tr in _children(table, "tr"):
        cells = []
        for tc in _children(tr, "tc"):
            cell_size = _child(tc, "cellSz")
            span = _child(tc, "cellSpan")
            sub = _child(tc, "subList")
            paragraphs = []
            for paragraph in _children(sub, "p") if sub is not None else []:
                paragraphs.append({
                    "align": aligns.get(str(paragraph.get("paraPrIDRef")), "center"),
                    "runs": _runs(paragraph, chars),
                    "element": paragraph,
                })
            cells.append({
                "width_pt": _pt(cell_size.get("width") if cell_size is not None else 0),
                "height_pt": _pt(cell_size.get("height") if cell_size is not None else 0),
                "colspan": int((span.get("colSpan") if span is not None else None) or 1),
                "rowspan": int((span.get("rowSpan") if span is not None else None) or 1),
                "valign": _VALIGN.get((sub.get("vertAlign") if sub is not None else "") or "", "center"),
                "paragraphs": paragraphs,
            })
        rows.append(cells)
    return {
        "inline": pos is not None and pos.get("treatAsChar") == "1",
        "horz_rel": (pos.get("horzRelTo") if pos is not None else "") or "PARA",
        "vert_rel": (pos.get("vertRelTo") if pos is not None else "") or "PARA",
        "horz_offset_pt": _pt(pos.get("horzOffset") if pos is not None else 0),
        "vert_offset_pt": _pt(pos.get("vertOffset") if pos is not None else 0),
        "width_pt": _pt(size.get("width") if size is not None else 0),
        "height_pt": _pt(size.get("height") if size is not None else 0),
        "rows": rows,
    }


def _restore_spilled_text(paragraph: dict[str, Any], tables: list[dict[str, Any]], style: dict) -> None:
    """변환기가 표 칸에 붙인 문단 글자를 문단으로 되돌린다(모듈 주석).

    글자는 표의 마지막 글자 있는 칸에 그 칸 글자 뒤 조각으로 붙는다(예: '국장' 칸의
    `<t>국장</t><t>보도자료</t>`). 문단에 제 글자가 있으면 건드리지 않는다.
    """
    if paragraph["runs"] or not tables:
        return
    cells = [cell for row in tables[-1]["rows"] for cell in row]
    for cell in reversed(cells):
        for cell_paragraph in reversed(cell["paragraphs"]):
            run = next(reversed(_children(cell_paragraph["element"], "run")), None)
            texts = ["".join(part.itertext()) for part in _children(run, "t")] if run is not None else []
            texts = [text for text in texts if text]
            if not texts:
                continue
            spilled = texts[1:]
            if not spilled:
                return  # 마지막 글자 있는 칸이 제 글자뿐이면 붙은 글자가 없다
            # `_runs` 는 글자 조각마다 한 항목이라 마지막 run 의 뒤 조각이 목록 끝에 있다.
            del cell_paragraph["runs"][len(cell_paragraph["runs"]) - len(spilled):]
            paragraph["runs"] = [{"text": "".join(spilled), "style": style}]
            return


def _textbox(shape: ET.Element, chars: dict[str, dict], aligns: dict[str, str]) -> dict[str, Any] | None:
    draw_text = _child(shape, "drawText")
    if draw_text is None:
        return None
    sub = _child(draw_text, "subList")
    margin = _child(draw_text, "textMargin")
    paragraphs: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    for paragraph in _children(sub, "p") if sub is not None else []:
        own_tables = [
            _table(table, chars, aligns)
            for run in _children(paragraph, "run") for table in _children(run, "tbl")
        ]
        entry = {
            "align": aligns.get(str(paragraph.get("paraPrIDRef")), "left"),
            "runs": _runs(paragraph, chars),
        }
        table_run = next(
            (run for run in _children(paragraph, "run") if _children(run, "tbl")), None,
        )
        style = chars.get(str(table_run.get("charPrIDRef")), {}) if table_run is not None else {}
        _restore_spilled_text(entry, own_tables, style)
        paragraphs.append(entry)
        tables.extend(own_tables)
    line = _child(shape, "lineShape")
    border = None
    if line is not None and (line.get("style") or "NONE") != "NONE":
        border = {"color": line.get("color") or "#000000", "width_pt": max(0.5, _pt(line.get("width")))}
    brush = _child(shape, "fillBrush")
    win = _child(brush, "winBrush") if brush is not None else None
    fill = win.get("faceColor") if win is not None and win.get("faceColor") not in {None, "none"} else None
    return {
        "margin_pt": [_pt(margin.get(key)) if margin is not None else 0.0
                      for key in ("top", "right", "bottom", "left")],
        "valign": _VALIGN.get((sub.get("vertAlign") if sub is not None else "") or "", "flex-start"),
        "paragraphs": paragraphs,
        "tables": tables,
        "border": border,
        "fill": fill,
    }


def _members(group: ET.Element, chars: dict, aligns: dict, binaries: dict[str, str]) -> list[dict]:
    """묶음 개체 안의 개체들(중첩 묶음은 펼친다)."""
    members: list[dict] = []
    for shape in group:
        kind = _local(shape.tag)
        if kind not in _SHAPES:
            continue
        if kind == "container":
            members.extend(_members(shape, chars, aligns, binaries))
            continue
        box, flip_x, flip_y = _member_box(shape)
        members.extend(_shape_items(shape, kind, box, flip_x, flip_y, chars, aligns, binaries))
    return members


def _shape_items(
    shape: ET.Element, kind: str, box: list[float], flip_x: bool, flip_y: bool,
    chars: dict, aligns: dict, binaries: dict[str, str],
) -> list[dict]:
    if kind == "pic":
        image = next((e for e in shape.iter() if _local(e.tag) == "img" and e.get("binaryItemIDRef")), None)
        binary = image.get("binaryItemIDRef").lower() if image is not None else ""
        if binary not in binaries:
            return []
        return [{"kind": "image", "box": box, "binary": binary, "flip_x": flip_x, "flip_y": flip_y}]
    if kind == "line":
        line = _child(shape, "lineShape")
        if line is None or (line.get("style") or "NONE") == "NONE":
            return []
        start, end = _child(shape, "startPt"), _child(shape, "endPt")
        reverse = (start is not None and end is not None
                   and (_num(start.get("y")) > _num(end.get("y"))) != (_num(start.get("x")) > _num(end.get("x"))))
        return [{"kind": "line", "box": box, "reverse": reverse,
                 "color": line.get("color") or "#000000", "width_pt": max(0.5, _pt(line.get("width")))}]
    if kind in _TEXT_SHAPES:
        textbox = _textbox(shape, chars, aligns)
        return [{"kind": "textbox", "box": box, **textbox}] if textbox else []
    return []


def _has_text(member: dict[str, Any]) -> bool:
    """글상자 문단에 글자가 있는가. 글상자 안 떠 있는 표는 화면에 없으므로 세지 않는다."""
    return member["kind"] == "textbox" and any(
        run["text"].strip() for paragraph in member["paragraphs"] for run in paragraph["runs"]
    )


def _lines(paragraph: ET.Element) -> list[tuple[float, float]]:
    """문단 줄마다 (vertpos, vertsize + spacing) pt."""
    array = _child(paragraph, "linesegarray")
    if array is None:
        return []
    return [
        (_pt(line.get("vertpos")), _pt(line.get("vertsize")) + _pt(line.get("spacing")))
        for line in array if _local(line.tag) == "lineseg"
    ]


def _natural_tops(paragraphs: list[ET.Element]) -> list[tuple[int, float, float]]:
    """최상위 문단마다 (쪽 순번, 위, 아래) pt — 본문 위쪽 기준, 개체가 줄을 밀기 전 위치.

    한컴은 문단 기준 `자리 차지` 개체를 개체가 밀기 전의 문단 위치에 놓고, 겹치는 줄을 개체
    아래로 민다. 저장된 `vertpos` 는 밀린 뒤 값이라 개체 위치로 쓸 수 없다. 쪽 첫 문단의 줄
    위치에서 시작해 앞 문단 줄 높이(`vertsize + spacing`)를 쌓는다. 예금성 광고3: p1·p2 각
    19.2pt → p3 38.4pt, 묶음(-7.84pt) 30.56~149.41pt, 그 아래 끝이 밀린 p2 의 vertpos 149.41pt.
    쪽 시작은 hwp_pagination 과 같은 기준(앞 문단 마지막 줄보다 위이거나 같은 위치, 쪽 나눔)이다.
    """
    tops: list[tuple[int, float, float]] = []
    page, natural, last = 0, 0.0, None
    for index, paragraph in enumerate(paragraphs):
        lines = _lines(paragraph)
        first = lines[0][0] if lines else None
        page_break = paragraph.get("pageBreak") not in {None, "", "0", "false", "False"}
        if index and (page_break or (first is not None and last is not None and first <= last)):
            page += 1
            natural = first if first is not None else 0.0
        elif index == 0 and first is not None:
            natural = first
        top = natural
        natural += sum(height for _, height in lines)
        tops.append((page, top, natural))
        if lines:
            last = lines[-1][0]
    return tops


def read_drawings(data: bytes) -> list[dict[str, Any]]:
    """HWPX → 최상위 문단에 놓인 그리기 개체(묶음 개체·글상자) 목록, 문서 순서."""
    drawings: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        chars, aligns = _styles(archive)
        binaries = _binary_paths(archive)
        images: dict[str, str] = {}
        for s_idx, name in enumerate(_section_names(archive), start=1):
            section = ET.fromstring(archive.read(name))
            margins = _page_layout(section)["margins"]
            paragraphs = [e for e in section if _local(e.tag) == "p"]
            tops = _natural_tops(paragraphs)
            for p_idx, paragraph in enumerate(paragraphs, start=1):
                counter = 0
                for run in _children(paragraph, "run"):
                    for shape in run:
                        kind = _local(shape.tag)
                        if kind != "container" and kind not in _TEXT_SHAPES:
                            continue
                        pos, size = _child(shape, "pos"), _child(shape, "sz")
                        if pos is None or size is None:
                            continue
                        width, height = _pt(size.get("width")), _pt(size.get("height"))
                        if kind == "container":
                            members = _members(shape, chars, aligns, binaries)
                        else:
                            members = _shape_items(
                                shape, kind, [0.0, 0.0, width, height], False, False,
                                chars, aligns, binaries,
                            )
                        if not any(_has_text(m) for m in members):
                            continue  # 글자가 없는 장식 도형은 다루지 않는다
                        counter += 1
                        out_margin = _child(shape, "outMargin")
                        page_index, natural_top, _bottom = tops[p_idx - 1]
                        body_top = natural_top + _pt(pos.get("vertOffset"))
                        # 자리 차지 개체가 밀어낼 첫 문단: 같은 쪽에서 개체 위쪽보다 아래로 내려오는 문단.
                        spacer = next(
                            (index + 1 for index, (page_no, _top, bottom) in enumerate(tops[:p_idx])
                             if page_no == page_index and bottom > body_top + 0.01),
                            p_idx,
                        )
                        drawings.append({
                            "anchor": f"s{s_idx}.p{p_idx}",
                            "drawing_id": f"s{s_idx}.p{p_idx}.d{counter}",
                            "section": s_idx,
                            "margins": margins,
                            "kind": kind,
                            "wrap": shape.get("textWrap") or "",
                            "inline": pos.get("treatAsChar") == "1",
                            "horz_rel": pos.get("horzRelTo") or "PARA",
                            "vert_rel": pos.get("vertRelTo") or "PARA",
                            "horz_align": pos.get("horzAlign") or "LEFT",
                            "vert_align": pos.get("vertAlign") or "TOP",
                            "horz_offset_pt": _pt(pos.get("horzOffset")),
                            "vert_offset_pt": _pt(pos.get("vertOffset")),
                            "width_pt": width,
                            "height_pt": height,
                            "out_margin_pt": [
                                _pt(out_margin.get(key)) if out_margin is not None else 0.0
                                for key in ("top", "right", "bottom", "left")
                            ],
                            "natural_top_pt": round(natural_top, 2),
                            "spacer_anchor": f"s{s_idx}.p{spacer}",
                            "members": members,
                        })
                        for member in members:
                            binary = member.get("binary")
                            if binary and binary not in images:
                                path = binaries[binary]
                                mime = _MIME.get("." + path.rsplit(".", 1)[-1].lower(), "application/octet-stream")
                                images[binary] = f"data:{mime};base64,{base64.b64encode(archive.read(path)).decode()}"
        for drawing in drawings:
            for member in drawing["members"]:
                if member.get("binary"):
                    member["src"] = images.get(member["binary"], "")
    return drawings


# ── HTML ─────────────────────────────────────────────────────────────────

_STYLE = """
<style id="nh-hwp-drawing-style">
.document-page { position: relative !important; }
.nh-hwp-drawing { position: absolute; z-index: 2; box-sizing: border-box; overflow: visible; }
.nh-hwp-drawing.nh-hwp-drawing--back { z-index: 0; }
.nh-hwp-drawing > * { position: absolute; box-sizing: border-box; margin: 0; }
.nh-hwp-textbox { display: flex; flex-direction: column; overflow: visible; }
.nh-hwp-textbox > p { margin: 0; line-height: 1.25; white-space: pre-wrap; }
.nh-hwp-drawing-spacer { display: block; height: 0; margin: 0; padding: 0; }
</style>
"""


def _span(run: dict[str, Any]) -> str:
    style = run.get("style") or {}
    css = [f"font-size:{style.get('size_pt', 10)}pt", f"color:{style.get('color', '#000000')}"]
    if style.get("bold"):
        css.append("font-weight:bold")
    if style.get("italic"):
        css.append("font-style:italic")
    if style.get("spacing_em"):
        css.append(f"letter-spacing:{style['spacing_em']:.2f}em")
    return f'<span style="{";".join(css)}">{html_lib.escape(run["text"])}</span>'


def _box_css(box: list[float]) -> str:
    return (f"left:{box[0]:.2f}pt;top:{box[1]:.2f}pt;"
            f"width:{box[2] - box[0]:.2f}pt;height:{box[3] - box[1]:.2f}pt")


def _member_html(drawing: dict[str, Any], member: dict[str, Any], index: int) -> tuple[int, str]:
    """(쌓는 순서, HTML). 그림 → 선 → 글상자 순으로 위에 쌓인다."""
    box = member["box"]
    if member["kind"] == "image":
        flips = []
        if member["flip_x"]:
            flips.append("scaleX(-1)")
        if member["flip_y"]:
            flips.append("scaleY(-1)")
        transform = f";transform:{' '.join(flips)}" if flips else ""
        return 0, (f'<img alt="" style="{_box_css(box)}{transform}" '
                   f'src="{html_lib.escape(member.get("src", ""), quote=True)}">')
    if member["kind"] == "line":
        stroke = member["width_pt"]
        # 가로·세로 선은 높이·너비가 0 이라 SVG 로는 그려지지 않는다. 테두리 한 변으로 그린다.
        if box[3] - box[1] < 0.5:
            return 1, (f'<div style="left:{box[0]:.2f}pt;top:{box[1] - stroke / 2:.2f}pt;'
                       f'width:{box[2] - box[0]:.2f}pt;height:0;'
                       f'border-top:{stroke:.2f}pt solid {member["color"]}"></div>')
        if box[2] - box[0] < 0.5:
            return 1, (f'<div style="left:{box[0] - stroke / 2:.2f}pt;top:{box[1]:.2f}pt;'
                       f'width:0;height:{box[3] - box[1]:.2f}pt;'
                       f'border-left:{stroke:.2f}pt solid {member["color"]}"></div>')
        width, height = box[2] - box[0], box[3] - box[1]
        y1, y2 = (height, 0.0) if member["reverse"] else (0.0, height)
        return 1, (
            f'<svg style="{_box_css(box)};overflow:visible" '
            f'viewBox="0 0 {width:.2f} {height:.2f}" preserveAspectRatio="none">'
            f'<line x1="0" y1="{y1:.2f}" x2="{width:.2f}" y2="{y2:.2f}" '
            f'stroke="{member["color"]}" stroke-width="{member["width_pt"]:.2f}" '
            'vector-effect="non-scaling-stroke"/></svg>'
        )
    top, right, bottom, left = member["margin_pt"]
    css = [_box_css(box), f"padding:{top:.2f}pt {right:.2f}pt {bottom:.2f}pt {left:.2f}pt",
           f"justify-content:{member['valign']}"]
    if member["border"]:
        css.append(f"border:{member['border']['width_pt']:.2f}pt solid {member['border']['color']}")
    if member["fill"]:
        css.append(f"background:{member['fill']}")
    node = f"drawing:{drawing['drawing_id']}:{index}"
    paragraphs = "".join(
        f'<p data-node-id="{node}:{order}" style="text-align:{paragraph["align"]}">'
        f'{"".join(_span(run) for run in paragraph["runs"]) or "&nbsp;"}</p>'
        for order, paragraph in enumerate(member["paragraphs"])
    )
    return 2, f'<div class="nh-hwp-textbox" data-nh-drawing="{node}" style="{";".join(css)}">{paragraphs}</div>'


def _paper_box(drawing: dict[str, Any], page: Any) -> list[float]:
    """개체 → 용지 좌상단 기준 [x0, y0, x1, y1] pt. 문단 기준 축은 밀리기 전 문단 위치를 쓴다."""
    margins = drawing["margins"]
    left, top, _right, _bottom = _box_pt(drawing, page, margins)
    if drawing["horz_rel"] not in {"PAPER", "PAGE"}:
        left = margins.get("left", 0.0) + drawing["horz_offset_pt"]
    if drawing["vert_rel"] not in {"PAPER", "PAGE"}:
        top = (margins.get("top", 0.0) + margins.get("header", 0.0)
               + drawing["natural_top_pt"] + drawing["vert_offset_pt"])
    return [round(left, 2), round(top, 2),
            round(left + drawing["width_pt"], 2), round(top + drawing["height_pt"], 2)]


def inject_drawings(
    html: str, docir: Any, drawings: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """그리기 개체를 쪽 겹으로 넣고, 자리 차지 개체 앞에 본문을 밀 빈 상자를 둔다. (HTML, 기록)"""
    if not drawings:
        return html, []
    from document_processor import paragraph_page_number

    order = {}
    for paragraph in docir.paragraphs:
        path = getattr(getattr(paragraph, "native_anchor", None), "debug_path", None) or ""
        match = re.fullmatch(r"s(\d+)\.p(\d+)", path)
        if match and paragraph.node_id:
            order[(int(match[1]), int(match[2]))] = paragraph

    def paragraph_at(path: str) -> Any:
        # 기준 문단이 빈 문단으로 빠졌으면 같은 구역의 다음 문단을 쓴다.
        s_idx, p_idx = (int(value) for value in re.findall(r"\d+", path)[:2])
        return next((order[key] for key in sorted(order) if key[0] == s_idx and key[1] >= p_idx), None)

    pages = {int(page.page_number): page for page in getattr(docir, "pages", None) or []}
    records = []
    for drawing in drawings:
        anchor = paragraph_at(drawing["anchor"])
        if anchor is None:
            continue
        page_no = paragraph_page_number(anchor) or 1
        section = re.compile(rf'(<section class="document-page" data-page-number="{page_no}"[^>]*>)')
        if not section.search(html):
            continue
        box = _paper_box(drawing, pages.get(page_no))
        members = sorted(
            (_member_html(drawing, member, index) for index, member in enumerate(drawing["members"])),
            key=lambda pair: pair[0],
        )
        # 그림·선은 본문 뒤, 글상자는 본문 앞에 둔다. 본문 배치가 한컴과 조금 달라 겹쳐도
        # 본문 글자가 그림에 가리지 않는다(예금성 광고3 1쪽 결재 도장 표와 띠 그림).
        back = "".join(part for layer, part in members if layer < 2)
        front = "".join(part for layer, part in members if layer >= 2)
        data = f'data-nh-drawing="{drawing["drawing_id"]}" style="{_box_css(box)}"'
        fragment = (f'<div class="nh-hwp-drawing nh-hwp-drawing--back" {data}>{back}</div>' if back else "") + (
            f'<div class="nh-hwp-drawing" {data}>{front}</div>' if front else "")
        html = section.sub(lambda m: m.group(1) + fragment, html, count=1)
        spacer_before = None
        if drawing["wrap"] == "TOP_AND_BOTTOM" or drawing["inline"]:
            target = paragraph_at(drawing["spacer_anchor"]) or anchor
            marker = f'<p data-node-id="{html_lib.escape(target.node_id)}"'
            if marker in html:
                until = box[3] + drawing["out_margin_pt"][2]
                html = html.replace(
                    marker,
                    f'<div class="nh-hwp-drawing-spacer" data-nh-until-pt="{until:.2f}"></div>' + marker,
                    1,
                )
                spacer_before = drawing["spacer_anchor"]
        records.append({
            "kind": drawing["kind"],
            "drawing_id": drawing["drawing_id"],
            "page_no": page_no,
            "wrap": drawing["wrap"],
            "box_pt": box,
            "spacer_before": spacer_before,
            "text": [
                "".join(run["text"] for run in paragraph["runs"])
                for member in drawing["members"] if member["kind"] == "textbox"
                for paragraph in member["paragraphs"]
            ],
        })
    if records:
        html = html.replace("</head>", f"{_STYLE}\n</head>", 1)
    return html, records
