"""document-processor 가 놓치는 HWP 쪽 나눔을 원본 줄 위치로 바로잡는다.

한컴은 저장할 때 문단마다 마지막 조판 결과(`hp:linesegarray/hp:lineseg` 의 `vertpos`,
본문 위쪽에서 잰 줄 위치)를 남긴다. document-processor 는 "다음 문단의 첫 줄이 앞 문단의
마지막 줄보다 **위**에서 시작하면 새 쪽"으로 쪽을 나눈다(`document_ir_parser`,
`vertpos < last_vertpos`).

이 규칙은 앞 문단이 쪽 맨 위(`vertpos=0`)에서 시작해 한 쪽을 다 채운 경우를 놓친다.
예금성 광고3(2026-10-15)은 3쪽 요약 표 문단(`s1.p21`, 0pt, 높이 740pt)이 쪽을 채우고
다음 문단(`s1.p22`)이 다시 0pt 에서 4쪽을 시작한다. 같은 위치라 "위"가 아니어서 표·
`[보도자료 이미지]`·사진이 모두 3쪽에 들어갔고, 용지 기준으로 놓는 사진 겹(hwp_layers)이
3쪽 맨 위 요약 표를 덮었다.

같은 쪽의 두 문단이 같은 줄 위치에서 시작할 수는 없으므로, 앞 문단 마지막 줄과
**같은** 위치에서 시작하는 문단도 새 쪽으로 본다. document-processor 의 판단은 그대로 두고
놓친 쪽 시작만 더한다. 단(段)이 여럿인 구역은 둘째 단도 위에서 다시 시작하므로 건드리지
않는다. 라이브러리가 고쳐지면 이 모듈을 지운다(`HWP_LINESEG_PAGINATION=off` 로 끌 수 있다).
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


@dataclass
class HwpPreparation:
    """한 문서의 구조 추출→렌더 준비 사이에서만 성공한 해석을 공유한다.

    렌더 준비는 DocIR을 변경하므로 호출측은 먼저 원문 구조를 값으로 추출해야 한다.
    파싱 요청 밖에 보관하거나 다른 문서에 재사용하지 않는다.
    """

    path: Path
    hwpx: bytes | None = field(default=None, init=False, repr=False)
    _loaded: tuple[Any, dict[str, Any]] | None = field(default=None, init=False, repr=False)
    _hwpx_read: bool = field(default=False, init=False, repr=False)

    def load(self, path: Path) -> tuple[Any, dict[str, Any]]:
        if path.resolve() != self.path.resolve():
            raise ValueError("HWP 해석 결과는 같은 입력 문서 안에서만 재사용할 수 있습니다")
        if self._loaded is None:
            if not self._hwpx_read:
                from .hwp_layers import hwpx_bytes

                try:
                    self.hwpx = hwpx_bytes(path)
                except Exception:  # noqa: BLE001 — 기존 쪽 보정·겹 복원에서 실패를 기록한다
                    self.hwpx = None
                self._hwpx_read = True
            self._loaded = load_paged_docir(path, self.hwpx)
        return self._loaded


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _section_names(archive: zipfile.ZipFile) -> list[str]:
    names = [n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
    return sorted(names, key=lambda name: int(re.findall(r"\d+", name)[-1]))


def _multi_column(section: ET.Element) -> bool:
    return any(
        _local(e.tag) == "colPr" and (_int(e.get("colCount")) or 1) > 1 for e in section.iter()
    )


def _line_vertpos(paragraph: ET.Element) -> list[int]:
    array = next((e for e in paragraph if _local(e.tag) == "linesegarray"), None)
    if array is None:
        return []
    values = (_int(e.get("vertpos")) for e in array if _local(e.tag) == "lineseg")
    return [value for value in values if value is not None]


def missed_page_starts(hwpx: bytes) -> list[str]:
    """document-processor 가 놓친 쪽 시작 문단의 경로(`s1.p22` …), 문서 순서대로."""
    starts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(hwpx)) as archive:
        for s_idx, name in enumerate(_section_names(archive), start=1):
            section = ET.fromstring(archive.read(name))
            if _multi_column(section):
                continue
            last_vertpos: int | None = None
            seen = False
            paragraphs = (e for e in section if _local(e.tag) == "p")
            for p_idx, paragraph in enumerate(paragraphs, start=1):
                # document-processor 와 같은 순서로 앞 문단 위치를 추적한다.
                if seen and paragraph.get("pageBreak") not in {None, "", "0", "false", "False"}:
                    last_vertpos = None
                lines = _line_vertpos(paragraph)
                if seen and lines and last_vertpos is not None and lines[0] == last_vertpos:
                    starts.append(f"s{s_idx}.p{p_idx}")
                if lines:
                    last_vertpos = lines[-1]
                seen = True
    return starts


def _order(path: str | None) -> tuple[int, int] | None:
    match = re.fullmatch(r"s(\d+)\.p(\d+)", path or "")
    return (int(match[1]), int(match[2])) if match else None


def _shift_paragraph(paragraph: Any, offset: int, used: dict[int, int]) -> None:
    from document_processor import ImageIR, RunIR, TableIR

    for node in paragraph.content:
        if isinstance(node, (RunIR, ImageIR, TableIR)) and node.page_number is not None:
            used.setdefault(node.page_number + offset, node.page_number)
            node.page_number += offset
        if isinstance(node, TableIR):
            for cell in node.iter_cells():
                for cell_paragraph in cell.paragraphs:
                    _shift_paragraph(cell_paragraph, offset, used)


def apply_page_starts(docir: Any, starts: list[str]) -> int:
    """놓친 쪽 시작마다 그 문단부터 뒤의 쪽 번호를 한 쪽씩 민다. 늘어난 쪽 수를 돌려준다."""
    breaks = sorted(order for order in map(_order, starts) if order is not None)
    if not breaks or not getattr(docir, "pages", None):
        return 0
    templates = {page.page_number: page for page in docir.pages}
    used: dict[int, int] = {}  # 새 쪽 번호 → 쪽 크기·여백을 빌려 올 원래 쪽 번호
    for paragraph in docir.paragraphs:
        order = _order(getattr(getattr(paragraph, "native_anchor", None), "debug_path", None))
        offset = sum(1 for point in breaks if order is not None and point <= order)
        _shift_paragraph(paragraph, offset, used)
    last = max([*used, *templates])
    pages = []
    for number in range(1, last + 1):
        source = used.get(number) or max(
            (old for old in templates if old <= number), default=min(templates),
        )
        pages.append(templates[source].model_copy(update={"page_number": number}))
    added = len(pages) - len(docir.pages)
    docir.pages = pages
    return added


def load_paged_docir(path: Any, hwpx: bytes | None = None) -> tuple[Any, dict[str, Any]]:
    """DocIR 을 읽고 놓친 쪽 나눔을 바로잡는다. (DocIR, manifest 에 남길 보정 기록)

    보정에 실패해도 document-processor 의 쪽 나눔 그대로 진행한다.
    """
    from document_processor import DocIR

    from .hwp_layers import hwpx_bytes

    docir = DocIR.from_file(str(path))
    record: dict[str, Any] = {"page_starts_added": [], "error": None}
    if os.environ.get("HWP_LINESEG_PAGINATION", "on").strip().lower() in {"off", "false", "0"}:
        record["error"] = "disabled"
        return docir, record
    try:
        starts = missed_page_starts(hwpx if hwpx is not None else hwpx_bytes(path))
        if apply_page_starts(docir, starts):
            record["page_starts_added"] = starts
    except Exception as exc:  # noqa: BLE001 — 쪽 보정 실패가 HWP 처리를 막지 않는다
        record["error"] = f"{type(exc).__name__}: {exc}"
    return docir, record
