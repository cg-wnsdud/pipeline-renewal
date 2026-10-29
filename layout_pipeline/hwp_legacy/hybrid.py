"""기존 parser-pipeline run.py 의 HWP hybrid 경로 함수(구조 Region + Paddle 시각 블록 합치기)를 그대로 옮긴 것.

출처: nh-ad-compliance dev 186ddca parser-pipeline/run.py 108-325행(_axis_share ~ _attach_visual_supplements). 함수 본문은 바꾸지 않는다.
"""
from __future__ import annotations

import re
from collections import Counter


def _axis_share(a0: int, a1: int, b0: int, b1: int) -> float:
    return max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1 - a0, b1 - b0))


def _normalize(value) -> str:
    return "".join(str(value or "").split()).casefold()


def character_coverage(left: str, right: str) -> float:
    """OCR 오탈자를 허용하며 left가 right로 얼마나 설명되는지 계산한다."""
    if not left or not right:
        return 0.0
    shared = sum((Counter(left) & Counter(right)).values())
    return shared / len(left)


def _novel_visual_blocks(
    visual: list[dict], structured: list[dict],
) -> tuple[list[dict], int]:
    """구조 Region에 이미 표현된 Paddle 블록을 버리고 시각 전용 요소만 남긴다."""
    normalize = _normalize

    kept: list[dict] = []
    discarded = 0
    for block in visual:
        # Paddle의 표 HTML은 OCR 텍스트와 태그가 섞인 별도 추정 결과다. HWP DOM이
        # 이미 원본 표 행을 제공할 때 이를 한 행의 새 본문으로 붙이면 중복·태그가
        # P3까지 유출된다. 시각 요소 보완에는 순수 텍스트 블록만 사용한다.
        if "<table" in str(block.get("content") or "").casefold() and any(
            item.get("kind") == "table" and item.get("table") for item in structured
        ):
            discarded += 1
            continue
        bbox = block.get("bbox") or []
        text = normalize(block.get("content"))
        duplicate = False
        enclosed_texts: list[str] = []
        if len(bbox) == 4:
            bx0, by0, bx1, by1 = (int(value) for value in bbox)
            block_area = max(1, (bx1 - bx0) * (by1 - by0))
            for known in structured:
                other = known.get("bbox") or []
                if len(other) != 4:
                    continue
                ax0, ay0, ax1, ay1 = (int(value) for value in other)
                x_share = _axis_share(ax0, ax1, bx0, bx1)
                y_share = _axis_share(ay0, ay1, by0, by1)
                if x_share < 0.8 or y_share < 0.8:
                    continue
                known_area = max(1, (ax1 - ax0) * (ay1 - ay0))
                intersection = (
                    max(0, min(ax1, bx1) - max(ax0, bx0))
                    * max(0, min(ay1, by1) - max(ay0, by0))
                )
                known_text = normalize(known.get("content"))
                if intersection / known_area >= 0.8 and known_text:
                    enclosed_texts.append(known_text)
                area_ratio = min(block_area, known_area) / max(block_area, known_area)
                same_geometry = x_share >= 0.95 and y_share >= 0.95 and area_ratio >= 0.75
                text_already_present = bool(text and known_text and text in known_text)
                same_text_with_ocr_noise = (
                    x_share >= 0.9
                    and y_share >= 0.9
                    and len(text) >= 8
                    and character_coverage(text, known_text) >= 0.8
                    and (
                        character_coverage(known_text, text) >= 0.8
                        # 시각 블록이 구조 행의 일부만 잘라 잡은 경우도 중복이다.
                        or character_coverage(text, known_text) >= 0.85
                    )
                )
                if same_geometry or text_already_present or same_text_with_ocr_noise:
                    duplicate = True
                    break
            # Paddle은 HWP 한 페이지의 표 전체를 text 블록 하나로 되돌려주기도 한다.
            # 구조 파서는 이미 그 안을 행별 exact/display_exact bbox로 나눴으므로, 큰
            # 블록의 내용 대부분이 내부 행들의 합집합에 있으면 보완 요소가 아니라
            # 중복 컨테이너다. 문자 multiset을 쓰는 이유는 OCR의 줄 순서 뒤섞임과
            # 띄어쓰기 차이를 허용하기 위해서다.
            if (
                not duplicate
                and len(enclosed_texts) >= 3
                and len(text) >= 20
                and character_coverage(text, "".join(enclosed_texts)) >= 0.72
            ):
                duplicate = True
        if duplicate:
            discarded += 1
        else:
            kept.append(block)
    return kept, discarded


def _alnum(value) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(value or "")).casefold()


def _structure_lines(structure: dict | None) -> list[str]:
    """HWP 원본 구조의 문단·칸 문구를 줄 단위로 편다. 띄어쓰기는 원본 그대로다."""
    lines: list[str] = []
    for paragraph in (structure or {}).get("paragraphs") or []:
        lines.extend(str(paragraph.get("text") or "").splitlines())
    for table in (structure or {}).get("tables") or []:
        for cell in table.get("cells") or []:
            lines.extend(str(cell.get("text") or "").splitlines())
    return list(dict.fromkeys(
        " ".join(line.split()) for line in lines if len(_alnum(line)) >= 4
    ))


def _structure_text_for(render: str, lines: list[str]) -> str | None:
    """렌더 글자가 가리키는 원본 줄을 찾는다. 렌더 텍스트층은 화면 줄바꿈대로
    단어 중간에서도 끊기므로 위치 확인에만 쓰고, 문구는 원본 줄을 쓴다."""
    target = _alnum(render)
    if not target:
        return None
    picked: list[tuple[int, int, str]] = []
    for line in sorted(lines, key=lambda value: -len(_alnum(value))):
        key = _alnum(line)
        start = target.find(key)
        if start < 0:
            continue
        end = start + len(key)
        if any(start < b and end > a for a, b, _ in picked):
            continue
        picked.append((start, end, line))
    covered = sum(b - a for a, b, _ in picked)
    if covered / len(target) < 0.8:
        return None
    return "\n".join(line for _, _, line in sorted(picked))


def _render_text_inside(bbox: list, digital_lines: list[dict]) -> str:
    """렌더 PDF 텍스트층에서 블록 안에 중심이 든 줄을 읽기 순서로 잇는다."""
    x0, y0, x1, y1 = (int(value) for value in bbox)
    inside = []
    for line in digital_lines:
        box = line.get("bbox") or []
        text = str(line.get("text") or "").strip()
        if len(box) != 4 or not text:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            inside.append((box[1], box[0], text))
    return "\n".join(text for _, _, text in sorted(inside))


def _attach_visual_supplements(
    visual: list[dict], structured: list[dict], digital_lines: list[dict] | None = None,
    structure: dict | None = None,
) -> tuple[list[dict], int]:
    """구조 표 행 안에서 Paddle이 찾은 새 문구를 HWP 원문으로만 보완한다.

    HWP 구조 행이 놓친 글자(예: 중첩 표 바깥의 각주 문단)는 원본 구조와 렌더 PDF
    텍스트층에 있다. OCR 문구를 붙이면 오독(`적용요을`)과 중복(`|지점명]…`)이 P3로
    새므로 쓰지 않는다.
    - 행 문구가 이미 담은 내용이면 버린다.
    - 블록 안 렌더 글자로 위치를 확인하고, 그 글자에 맞는 원본 구조 줄을 붙인다.
      원본 줄을 찾지 못하면 렌더 글자를 붙인다(화면 줄바꿈이 섞일 수 있음).
    - 렌더 글자가 없으면 이미지 속 글자다. 행에 붙이지 않고 별도 시각 Region으로 남긴다.
    OCR 문구는 P1 ``visual_supplements``에 위치와 함께 근거로만 남는다.
    """
    lines = _structure_lines(structure)
    free: list[dict] = []
    attached = 0
    for block in visual:
        bbox = block.get("bbox") or []
        text = str(block.get("content") or "").strip()
        candidates: list[tuple[int, dict]] = []
        if len(bbox) == 4 and text:
            bx0, by0, bx1, by1 = (int(value) for value in bbox)
            block_area = max(1, (bx1 - bx0) * (by1 - by0))
            for known in structured:
                if known.get("kind") != "table" or not known.get("table"):
                    continue
                other = known.get("bbox") or []
                if len(other) != 4:
                    continue
                ax0, ay0, ax1, ay1 = (int(value) for value in other)
                intersection = (
                    max(0, min(ax1, bx1) - max(ax0, bx0))
                    * max(0, min(ay1, by1) - max(ay0, by0))
                )
                if intersection / block_area >= 0.8:
                    candidates.append((max(1, (ax1 - ax0) * (ay1 - ay0)), known))
        if not candidates:
            free.append(block)
            continue
        _, target = min(candidates, key=lambda item: item[0])
        record = {"bbox": list(bbox), "ocr_text": text, "source": "paddlex_visual"}
        if character_coverage(_normalize(text), _normalize(target.get("content"))) >= 0.8:
            record["action"] = "duplicate_dropped"
            target.setdefault("visual_supplements", []).append(record)
            attached += 1
            continue
        render = _render_text_inside(bbox, digital_lines or [])
        if not render:
            record["action"] = "kept_as_image_text"
            target.setdefault("visual_supplements", []).append(record)
            free.append(block)
            continue
        original = _structure_text_for(render, lines)
        text, kind = (original, "structure_text") if original else (render, "render_text")
        record.update(render_text=render, text=text)
        if _alnum(text) in _alnum(target.get("content")):
            record["action"] = "duplicate_dropped"
        else:
            target["content"] = f"{str(target.get('content') or '').rstrip()}\n{text}".strip()
            target["text_source"] = f"document_processor_html_with_{kind}"
            table = target.setdefault("table", {})
            notes = table.setdefault("notes", [])
            if text not in notes:
                notes.append(text)
            record["action"] = f"{kind}_attached"
        target.setdefault("visual_supplements", []).append(record)
        attached += 1
    return free, attached

