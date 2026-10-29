"""HWP 쪽 배경과 떠 있는 그림을 검토 HTML에 되살린다.

document-processor(DocIR)는 두 가지를 읽지 않는다(2026-10-02, 라이브러리 코드 확인).

1) **쪽 배경** — `secPr/pageBorderFill` → header.xml `borderFill/fillBrush/imgBrush`.
   DocIR `PageInfo` 에 배경 필드가 없어 HTML 에서 통째로 빠진다. 대출성 샘플 7건
   (002·003·005·007·008·009·010)의 `금융의 모든 순간`·`NH농협금융` 배경이 이 경우다.
2) **떠 있는 그림의 배치** — `hp:pic` 의 `textWrap`·`hp:pos`. `ImageIR.placement` 필드는
   있지만 채우는 파서가 없어 모든 그림이 문단 흐름 속 `<img>` 가 된다. 용지 기준
   `글 뒤로` 배경(001·006)이 본문 아래에 한 쪽 크기로 붙고, `fitPages()` 가 그 높이에
   맞추느라 쪽 전체를 절반 가까이 줄였다.

HWPX 원본(HWP 는 document-processor 의 변환기로 HWPX 를 다시 만든다)에서 두 정보를
읽어, 떠 있는 그림은 DocIR 흐름에서 빼고 쪽 위의 절대 위치에 다시 놓는다. 겹(layer)은
`.document-page__content` 의 형제라 `fitPages()` 축소와 `data-node-id` bbox 계산에
끼지 않는다.
"""
from __future__ import annotations

import base64
import html as html_lib
import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_HWPUNIT_PER_PT = 100.0
# 흐름에서 빼서 절대 위치로 놓는 감싸기 방식. `어울림`(SQUARE)은 글이 그림을 비켜
# 흐르지만 DocIR HTML 은 원래 비켜 흐르기를 못 하므로, 흐름 속 엉뚱한 자리(001 QR 이
# 오른쪽 위 → 왼쪽 아래)보다 원래 자리가 낫다. `자리 차지`(TOP_AND_BOTTOM)는 위아래
# 글을 밀어내므로 흐름에 남긴다.
_FLOATING_WRAPS = {"BEHIND_TEXT", "IN_FRONT_OF_TEXT", "SQUARE"}
_ABSOLUTE_REL = {"PAPER", "PAGE"}
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local(child.tag) == name), None)


def _signed(value: str | None) -> float:
    """HWPUNIT 는 32비트 부호 있는 값을 부호 없이 적는다(4294966459 = -837)."""
    number = int(value or 0)
    if number >= 2 ** 31:
        number -= 2 ** 32
    return number / _HWPUNIT_PER_PT


def hwpx_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".hwpx":
        return path.read_bytes()
    from document_processor.core.hwp_converter import convert_hwp_to_hwpx_bytes

    return convert_hwp_to_hwpx_bytes(path)


def _binary_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    # document-processor `_hwpx_binary_name_map` 과 같은 규칙: BinData 파일 이름(확장자 제외).
    return {
        Path(name).stem.lower(): name
        for name in archive.namelist() if name.startswith("BinData/")
    }


def _image_fills(archive: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """header.xml borderFill id → 그림 채우기(imgBrush)."""
    try:
        root = ET.fromstring(archive.read("Contents/header.xml"))
    except KeyError:
        return {}
    fills: dict[str, dict[str, str]] = {}
    for border_fill in root.iter():
        if _local(border_fill.tag) != "borderFill":
            continue
        brush = next((e for e in border_fill.iter() if _local(e.tag) == "imgBrush"), None)
        if brush is None:
            continue
        image = next((e for e in brush.iter() if _local(e.tag) == "img"), None)
        if image is not None and image.get("binaryItemIDRef"):
            fills[str(border_fill.get("id"))] = {
                "binary": image.get("binaryItemIDRef").lower(),
                "mode": brush.get("mode") or "TOTAL",
            }
    return fills


def _section_names(archive: zipfile.ZipFile) -> list[str]:
    names = [n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
    return sorted(names, key=lambda name: int(re.findall(r"\d+", name)[-1]))


def _page_layout(section: ET.Element) -> dict[str, Any]:
    sec_pr = next((e for e in section.iter() if _local(e.tag) == "secPr"), None)
    layout: dict[str, Any] = {"page_fills": [], "visibility_fill": "SHOW_ALL", "margins": {}}
    if sec_pr is None:
        return layout
    visibility = _child(sec_pr, "visibility")
    if visibility is not None:
        layout["visibility_fill"] = visibility.get("fill") or "SHOW_ALL"
    page_pr = _child(sec_pr, "pagePr")
    margin = _child(page_pr, "margin") if page_pr is not None else None
    if margin is not None:
        layout["margins"] = {key: _signed(margin.get(key)) for key in margin.attrib}
    for fill in sec_pr:
        if _local(fill.tag) == "pageBorderFill":
            layout["page_fills"].append({
                "type": fill.get("type") or "BOTH",
                "border_fill": str(fill.get("borderFillIDRef")),
                "fill_area": fill.get("fillArea") or "PAPER",
            })
    return layout


def _floating_pictures(
    section: ET.Element, s_idx: int, binaries: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """최상위 문단에 직접 놓인 떠 있는 그림 → document-processor 이미지 경로.

    경로 규칙은 `document_ir_parser._parse_hwpx_paragraph_content` 를 따른다: 최상위
    `hp:p` 순번(1부터) + 문단 안에서 바이너리를 찾은 `run/pic` 순번 → `s1.p3.img1`.
    표 셀 안 그림은 다루지 않는다(샘플에서 셀 안 그림은 전부 흐름 속 로고였다).
    """
    found: dict[str, dict[str, Any]] = {}
    for p_idx, paragraph in enumerate((e for e in section if _local(e.tag) == "p"), start=1):
        counter = 0
        for run in (e for e in paragraph if _local(e.tag) == "run"):
            for pic in (e for e in run if _local(e.tag) == "pic"):
                image = next(
                    (e for e in pic.iter() if _local(e.tag) == "img" and e.get("binaryItemIDRef")),
                    None,
                )
                if image is None or image.get("binaryItemIDRef").lower() not in binaries:
                    continue
                counter += 1
                pos, size = _child(pic, "pos"), _child(pic, "sz")
                if pos is None or size is None or pos.get("treatAsChar") == "1":
                    continue
                wrap = pic.get("textWrap") or ""
                horz_rel, vert_rel = pos.get("horzRelTo") or "", pos.get("vertRelTo") or ""
                if wrap not in _FLOATING_WRAPS or {horz_rel, vert_rel} - _ABSOLUTE_REL:
                    continue
                found[f"s{s_idx}.p{p_idx}.img{counter}"] = {
                    "wrap": wrap,
                    "z_order": int(pic.get("zOrder") or 0),
                    "horz_rel": horz_rel,
                    "vert_rel": vert_rel,
                    "horz_align": pos.get("horzAlign") or "LEFT",
                    "vert_align": pos.get("vertAlign") or "TOP",
                    "horz_offset_pt": _signed(pos.get("horzOffset")),
                    "vert_offset_pt": _signed(pos.get("vertOffset")),
                    "width_pt": _signed(size.get("width")),
                    "height_pt": _signed(size.get("height")),
                    "binary": image.get("binaryItemIDRef").lower(),
                }
    return found


def read_layers(data: bytes) -> dict[str, Any]:
    """HWPX 바이트 → {sections: [...], floats: {경로: 배치}, images: {바이너리: data URL}}."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        binaries = _binary_paths(archive)
        fills = _image_fills(archive)
        sections: list[dict[str, Any]] = []
        floats: dict[str, dict[str, Any]] = {}
        used: set[str] = set()
        for s_idx, name in enumerate(_section_names(archive), start=1):
            root = ET.fromstring(archive.read(name))
            layout = _page_layout(root)
            for page_fill in layout["page_fills"]:
                brush = fills.get(page_fill["border_fill"])
                if brush and brush["binary"] in binaries:
                    page_fill.update(brush)
                    used.add(brush["binary"])
            layout["page_fills"] = [fill for fill in layout["page_fills"] if fill.get("binary")]
            sections.append(layout)
            floats.update(_floating_pictures(root, s_idx, binaries))
        used.update(spec["binary"] for spec in floats.values())
        images = {}
        for binary in used:
            path = binaries[binary]
            mime = _MIME.get(Path(path).suffix.lower(), "application/octet-stream")
            images[binary] = f"data:{mime};base64,{base64.b64encode(archive.read(path)).decode()}"
    return {"sections": sections, "floats": floats, "images": images}


def detach_floating_images(docir: Any, layers: dict[str, Any]) -> list[dict[str, Any]]:
    """떠 있는 그림을 DocIR 문단 흐름에서 빼고, 놓을 자리 목록을 돌려준다."""
    from document_processor import ImageIR, paragraph_page_number, set_paragraph_page_number

    floats = layers.get("floats") or {}
    placements: list[dict[str, Any]] = []
    kept_paragraphs = []
    for paragraph in docir.paragraphs:
        removed = []
        content = []
        for node in paragraph.content:
            anchor = getattr(node, "native_anchor", None)
            spec = floats.get(getattr(anchor, "debug_path", None)) if isinstance(node, ImageIR) else None
            if spec is None:
                content.append(node)
                continue
            removed.append(node)
        if not removed:
            kept_paragraphs.append(paragraph)
            continue
        page = paragraph_page_number(paragraph) or 1
        for node in removed:
            spec = floats[node.native_anchor.debug_path]
            placements.append({
                "kind": "floating_image",
                "page_no": int(node.page_number or page),
                "section": int(node.native_anchor.debug_path.split(".", 1)[0][1:]),
                "node_debug_path": node.native_anchor.debug_path,
                **spec,
            })
        paragraph.content = content
        # 그림만 들고 있던 문단은 쪽 번호를 잃어 `document-unpaged` 로 빠진다. 같은 쪽에 묶어 둔다.
        if paragraph_page_number(paragraph) is None:
            set_paragraph_page_number(paragraph, page)
        if paragraph_page_number(paragraph) is None and not (paragraph.text or "").strip():
            continue  # 남은 내용이 없다 — 빈 문단을 남기면 쪽 밖 영역이 생긴다
        kept_paragraphs.append(paragraph)
    docir.paragraphs = kept_paragraphs
    return placements


def page_fill_placements(docir: Any, layers: dict[str, Any]) -> list[dict[str, Any]]:
    """쪽 배경을 쪽마다 펼친다. BOTH 는 모든 쪽, ODD/EVEN 은 해당 쪽에만."""
    from document_processor import paragraph_page_number

    sections = layers.get("sections") or []
    if not any(section["page_fills"] for section in sections):
        return []
    page_section: dict[int, int] = {}
    for paragraph in docir.paragraphs:
        anchor = getattr(paragraph, "native_anchor", None)
        match = re.match(r"s(\d+)\.", getattr(anchor, "debug_path", "") or "")
        page = paragraph_page_number(paragraph)
        if match and page is not None:
            page_section.setdefault(int(page), int(match.group(1)))
    placements = []
    section_no, first_page_of = 1, {}
    for page in getattr(docir, "pages", None) or []:
        number = int(page.page_number)
        section_no = page_section.get(number, section_no)
        first_page_of.setdefault(section_no, number)
        if section_no - 1 >= len(sections):
            continue
        section = sections[section_no - 1]
        first = first_page_of[section_no] == number
        visibility = section["visibility_fill"]
        if (visibility == "HIDE_FIRST" and first) or (visibility == "SHOW_FIRST" and not first):
            continue
        parity = "ODD" if number % 2 else "EVEN"
        chosen = next(
            (fill for kind in ("BOTH", parity) for fill in section["page_fills"] if fill["type"] == kind),
            None,
        )
        if chosen is None:
            continue
        margins = section["margins"]
        placements.append({
            "kind": "page_fill",
            "page_no": number,
            "section": section_no,
            "fill_area": chosen["fill_area"],
            "mode": chosen["mode"],
            "binary": chosen["binary"],
            # PAPER 는 용지 전체, PAGE 는 여백 안쪽. BORDER 는 쪽 테두리 간격이 따로 있지만
            # 샘플에 없어 PAPER 로 둔다.
            "inset_pt": (
                [margins.get("left", 0.0), margins.get("top", 0.0),
                 margins.get("right", 0.0), margins.get("bottom", 0.0)]
                if chosen["fill_area"] == "PAGE" else [0.0, 0.0, 0.0, 0.0]
            ),
        })
    return placements


def _box_pt(spec: dict[str, Any], page: Any, margins: dict[str, float]) -> list[float]:
    """기준(PAPER/PAGE)·정렬·오프셋 → 용지 좌상단 기준 [x0, y0, x1, y1] pt."""
    paper_w = float(getattr(page, "width_pt", None) or 595.28)
    paper_h = float(getattr(page, "height_pt", None) or 841.89)

    def axis(rel: str, align: str, offset: float, size: float, paper: float, lo: float, hi: float) -> float:
        start, length = (0.0, paper) if rel == "PAPER" else (lo, paper - lo - hi)
        if align == "CENTER":
            return start + (length - size) / 2 + offset
        if align in {"RIGHT", "BOTTOM"}:
            return start + length - size - offset
        return start + offset

    width, height = spec["width_pt"], spec["height_pt"]
    left = axis(spec["horz_rel"], spec["horz_align"], spec["horz_offset_pt"], width, paper_w,
                margins.get("left", 0.0), margins.get("right", 0.0))
    top = axis(spec["vert_rel"], spec["vert_align"], spec["vert_offset_pt"], height, paper_h,
               margins.get("top", 0.0) + margins.get("header", 0.0),
               margins.get("bottom", 0.0) + margins.get("footer", 0.0))
    return [round(left, 2), round(top, 2), round(left + width, 2), round(top + height, 2)]


_LAYER_STYLE = """
<style id="nh-hwp-layer-style">
.document-page { position: relative !important; }
.document-page__content { position: relative; z-index: 1; }
img.nh-hwp-layer {
  position: absolute; pointer-events: none; max-width: none !important;
  margin: 0; border: 0; padding: 0;
}
</style>
"""


def inject_layers(
    html: str, docir: Any, layers: dict[str, Any], placements: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """쪽마다 배경·떠 있는 그림을 `<img class="nh-hwp-layer">` 로 넣는다."""
    if not placements:
        return html, []
    pages = {int(page.page_number): page for page in getattr(docir, "pages", None) or []}
    sections = layers.get("sections") or []
    images = layers.get("images") or {}
    by_page: dict[int, list[tuple[tuple[int, int], str]]] = {}
    recorded = []
    for item in placements:
        page = pages.get(item["page_no"])
        src = images.get(item["binary"])
        if page is None or not src:
            continue
        margins = sections[item["section"] - 1]["margins"] if item["section"] - 1 < len(sections) else {}
        if item["kind"] == "page_fill":
            left, top, right, bottom = item["inset_pt"]
            box = [left, top, float(page.width_pt or 595.28) - right, float(page.height_pt or 841.89) - bottom]
            z_index = 0
        else:
            box = _box_pt(item, page, margins)
            z_index = 0 if item["wrap"] == "BEHIND_TEXT" else 2
        style = (
            f"left:{box[0]:.2f}pt;top:{box[1]:.2f}pt;"
            f"width:{box[2] - box[0]:.2f}pt;height:{box[3] - box[1]:.2f}pt;z-index:{z_index}"
        )
        # 배경 → 글 뒤 그림 → 글 앞 그림 순으로 쌓이도록 정렬 열쇠를 둔다.
        order = (0 if item["kind"] == "page_fill" else 1 + (z_index > 0), item.get("z_order", 0))
        by_page.setdefault(item["page_no"], []).append((order, (
            f'<img class="nh-hwp-layer" data-nh-layer="{item["kind"]}" alt="" '
            f'style="{style}" src="{html_lib.escape(src, quote=True)}">'
        )))
        recorded.append({key: value for key, value in item.items() if key != "binary"} | {
            "binary": item["binary"], "box_pt": [round(value, 2) for value in box],
        })

    def add_layers(match: re.Match) -> str:
        number = int(match.group(1))
        items = sorted(by_page.get(number, []), key=lambda pair: pair[0])
        return match.group(0) + "".join(tag for _, tag in items)

    output = re.sub(
        r'<section class="document-page" data-page-number="(\d+)"[^>]*>', add_layers, html,
    )
    output = output.replace("</head>", f"{_LAYER_STYLE}\n</head>", 1)
    return output, recorded
