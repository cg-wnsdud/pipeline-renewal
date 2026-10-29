"""HWP를 최종 검토 화면과 같은 self-contained HTML surface로 만든다.

이 HTML은 원본 한컴 화면의 픽셀 복제가 아니라 DocIR의 문단/표/스타일을 재구성한
검토 화면이다. 대신 모든 요소에 안정적인 ``data-node-id``가 있고, 브라우저 DOM bbox와
심의 결과를 같은 좌표계에서 바로 연결할 수 있다.
"""
from __future__ import annotations

import json
import os
import html as html_lib
import re
import subprocess
from pathlib import Path
from typing import Any


_STYLE = """
<style id="nh-review-surface-style">
:root {
  --nh-page-width: __PAGE_WIDTH__pt;
  --nh-page-height: __PAGE_HEIGHT__pt;
}
.document-page {
  box-sizing: border-box !important;
  width: var(--nh-page-width) !important;
  height: var(--nh-page-height) !important;
  min-height: var(--nh-page-height) !important;
  overflow: hidden !important;
}
[data-node-id][data-nh-highlight="true"] {
  outline: 3px solid #ff9800 !important;
  outline-offset: 2px;
  background-color: rgba(255, 235, 59, 0.34) !important;
}
@media print {
  @page { size: __PAGE_WIDTH__pt __PAGE_HEIGHT__pt; margin: 0; }
  html, body { margin: 0 !important; padding: 0 !important; background: white !important; }
  .document-page {
    margin: 0 !important;
    box-shadow: none !important;
    /* Keep the measured inner width: removing this border rewraps paragraphs. */
    border-color: transparent !important;
    break-after: page;
    page-break-after: always;
  }
  .document-page:last-child { break-after: auto; page-break-after: auto; }
}
</style>
"""


_SCRIPT = r"""
<script id="nh-review-surface-api">
(() => {
  const elements = () => Array.from(document.querySelectorAll('[data-node-id]'));
  const pageOf = (element) => element.closest('.document-page');
  // 자리 차지 그리기 개체(hwp_drawings) 앞의 빈 상자를 개체 아래 끝까지 늘려 뒤 본문을 민다.
  // 쪽 축소(fitPages) 전에, 축소를 걷어 낸 배치에서 잰다.
  const placeSpacers = () => {
    document.querySelectorAll('.nh-hwp-drawing-spacer').forEach(spacer => {
      const page = pageOf(spacer);
      if (!page) return;
      const content = page.querySelector('.document-page__content');
      if (content) content.style.transform = '';
      spacer.style.height = '0px';
      const top = spacer.getBoundingClientRect().top - page.getBoundingClientRect().top;
      const until = Number(spacer.dataset.nhUntilPt || 0) * 4 / 3;
      spacer.style.height = `${Math.max(0, until - top)}px`;
    });
  };
  const fitPages = () => {
    placeSpacers();
    Array.from(document.querySelectorAll('.document-page')).forEach(page => {
      const content = page.querySelector('.document-page__content');
      if (!content) return;
      content.style.transform = '';
      content.style.transformOrigin = 'top left';
      const widthScale = page.clientWidth / Math.max(content.scrollWidth, 1);
      const heightScale = page.clientHeight / Math.max(content.scrollHeight, 1);
      const scale = Math.min(1, widthScale, heightScale);
      page.dataset.nhScale = String(scale);
      if (scale < 0.999) content.style.transform = `scale(${scale})`;
    });
  };
  const rectFor = (element) => {
    const rect = element.getBoundingClientRect();
    const page = pageOf(element);
    const pageRect = page ? page.getBoundingClientRect() : {left: 0, top: 0};
    return {
      node_id: element.dataset.nodeId,
      page_no: page ? Number(page.dataset.pageNumber || page.dataset.page || 1) : 1,
      bbox: [
        rect.left - pageRect.left,
        rect.top - pageRect.top,
        rect.right - pageRect.left,
        rect.bottom - pageRect.top,
      ].map(value => Math.round(value * 100) / 100),
      text: (element.innerText || element.textContent || '').trim(),
    };
  };
  const rowBoxes = () => {
    const rows = [];
    const pushValues = (nodeId, page, values) => {
      if (!values.length) return;
      const left = Math.min(...values.map(cell => cell.bbox[0]));
      const top = Math.min(...values.map(cell => cell.bbox[1]));
      const right = Math.max(...values.map(cell => cell.bbox[2]));
      const bottom = Math.max(...values.map(cell => cell.bbox[3]));
      rows.push({
        node_id: nodeId,
        page_no: page ? Number(page.dataset.pageNumber || page.dataset.page || 1) : 1,
        bbox: [left, top, right, bottom],
        text: values.map(cell => cell.text).join('\n'),
        cells: values,
      });
    };
    // 문단 단위 행. 빈 문단(빈 줄)도 구분자로 내보낸다 — 의미 단위 묶기는 Python
    // `dom_rows_to_blocks` 가 한다. index 는 부모의 자식 순번이라 사이에 표가 끼면 끊긴다.
    const pushParagraphs = (container, groupId, page, extra) => {
      const children = Array.from(container.children);
      Array.from(container.querySelectorAll(':scope > p[data-node-id]')).forEach(paragraph => {
        const rect = rectFor(paragraph);
        const value = {
          node_id: rect.node_id || '',
          col: extra.col || 0,
          col_span: extra.col_span || 1,
          row_span: 1,
          bbox: rect.bbox,
          text: rect.text,
        };
        const index = children.indexOf(paragraph);
        rows.push({
          node_id: `domparagraph:${value.node_id || groupId}:${index}`,
          page_no: page ? Number(page.dataset.pageNumber || page.dataset.page || 1) : 1,
          bbox: rect.bbox,
          text: rect.text,
          cells: rect.text ? [value] : [],
          paragraph: {group: groupId, index, blank: !rect.text},
        });
      });
    };
    // 표 밖 본문 문단. 예전에는 표 행만 읽어 LMS형 HWP(004·006·007 예금성)가 블록 1개로
    // 시각 경로(PaddleX 큰 상자)에 떨어졌다.
    document.querySelectorAll('.document-page').forEach((page, pageIndex) => {
      const content = page.querySelector('.document-page__content');
      if (content) pushParagraphs(content, `page${page.dataset.pageNumber || pageIndex + 1}`, page, {});
    });
    // 되살린 글상자(hwp_drawings) 문단. 본문 흐름 밖의 절대 위치라 따로 읽는다.
    document.querySelectorAll('.document-page .nh-hwp-textbox').forEach((box, boxIndex) => {
      pushParagraphs(box, box.dataset.nhDrawing || `drawing${boxIndex}`, pageOf(box), {});
    });
    document.querySelectorAll('.document-page table').forEach((table, tableIndex) => {
      Array.from(table.rows || []).forEach((row, rowIndex) => {
        const cells = Array.from(row.cells || []);
        // 중첩 표의 컨테이너 행은 자식 행의 원문을 다시 포함한다.
        if (cells.some(cell => cell.querySelector(':scope > table'))) return;
        const valueFor = (cell, colIndex) => {
          const rect = rectFor(cell);
          return {
            node_id: cell.dataset.nodeId || '',
            col: colIndex,
            col_span: Number(cell.colSpan || 1),
            row_span: Number(cell.rowSpan || 1),
            bbox: rect.bbox,
            text: (cell.innerText || cell.textContent || '').trim(),
          };
        };
        let values = cells.map(valueFor).filter(cell => cell.text);
        // 조판용 부모 행의 표제어가 왼쪽 셀에 있고 실제 값은 오른쪽 중첩 표에
        // 있는 경우, 자식 행에 부모 표제어 셀을 붙인다. 010의 '원금 및 이자
        // 상환방법'처럼 표제어를 잃지 않으면서 부모/자식 원문 중복도 만들지 않는다.
        const ownerCell = row.closest('td');
        if (ownerCell && ownerCell.parentElement) {
          const context = Array.from(ownerCell.parentElement.cells || [])
            .filter(cell => cell !== ownerCell && !cell.querySelector(':scope > table'))
            .map(valueFor)
            .filter(cell => cell.text);
          values = [...context, ...values];
        }
        if (!values.length) return;
        const page = pageOf(row);
        const height = Math.max(...values.map(cell => cell.bbox[3]))
          - Math.min(...values.map(cell => cell.bbox[1]));
        const largeCells = cells.filter(cell =>
          cell.querySelectorAll(':scope > p[data-node-id]').length >= 3
        );
        // 한 셀에 페이지 대부분의 본문을 넣은 LMS형 문서는 행 하나로 내보내지
        // 않는다. 화면에 실제 배치된 문단 bbox를 사용해 의미 단위 후보로 쪼갠다.
        if (page && height > page.clientHeight * 0.30 && largeCells.length) {
          const headerValues = cells
            .filter(cell => !largeCells.includes(cell))
            .flatMap((cell, colIndex) => {
              const paragraphs = Array.from(
                cell.querySelectorAll(':scope > p[data-node-id]')
              ).map(paragraph => valueFor(paragraph, colIndex)).filter(value => value.text);
              return paragraphs.length ? paragraphs : [valueFor(cell, colIndex)];
            })
            .filter(cell => cell.text);
          pushValues(
            `domrow:${table.dataset.nodeId || tableIndex}:${rowIndex}:heading`,
            page,
            headerValues,
          );
          largeCells.forEach((cell, cellIndex) => {
            pushParagraphs(cell, cell.dataset.nodeId || `cell${tableIndex}.${rowIndex}.${cellIndex}`,
              page, {col: cellIndex, col_span: Number(cell.colSpan || 1)});
          });
          return;
        }
        pushValues(
          `domrow:${table.dataset.nodeId || tableIndex}:${rowIndex}`,
          page,
          values,
        );
      });
    });
    return rows;
  };
  const publish = () => {
    let target = document.getElementById('nh-review-surface-data');
    if (!target) {
      target = document.createElement('script');
      target.id = 'nh-review-surface-data';
      target.type = 'application/json';
      document.body.appendChild(target);
    }
    target.textContent = JSON.stringify({rows: rowBoxes()});
  };
  window.nhReviewSurface = {
    fitPages,
    rows: rowBoxes,
    boxes() { return elements().map(rectFor); },
    highlight(nodeIds) {
      const selected = new Set(nodeIds || []);
      elements().forEach(element => {
        if (selected.has(element.dataset.nodeId)) element.dataset.nhHighlight = 'true';
        else delete element.dataset.nhHighlight;
      });
      return this.boxes().filter(item => selected.has(item.node_id));
    },
    clear() { return this.highlight([]); },
  };
  fitPages();
  publish();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {
    fitPages();
    publish();
  });
})();
</script>
"""


def inject_review_surface_api(
    html: str, *, page_width_pt: float = 595.28, page_height_pt: float = 841.89,
) -> str:
    output = html
    style = _STYLE.replace("__PAGE_WIDTH__", str(round(page_width_pt, 2))).replace(
        "__PAGE_HEIGHT__", str(round(page_height_pt, 2))
    )
    output = output.replace("</head>", f"{style}\n</head>", 1)
    output = output.replace("</body>", f"{_SCRIPT}\n</body>", 1)
    return output


def render_hwp_review_surface(path: Path, output_dir: Path, *, prepared=None) -> dict[str, Any]:
    """사내 파서의 clean review HTML과 표시 방식 메타데이터를 생성한다."""
    from document_processor import DocumentInput, render_review_html

    from .hwp_layers import (
        detach_floating_images, hwpx_bytes, inject_layers, page_fill_placements, read_layers,
    )
    from .hwp_pagination import load_paged_docir

    if prepared is not None:
        docir, pagination = prepared.load(path)
        hwpx = prepared.hwpx
    else:
        try:
            hwpx = hwpx_bytes(path)
        except Exception:  # noqa: BLE001 — 아래 겹 복원·쪽 보정이 각자 실패를 기록한다
            hwpx = None
        # document-processor 가 놓친 쪽 나눔을 원본 줄 위치로 바로잡는다(hwp_pagination 주석).
        docir, pagination = load_paged_docir(path, hwpx)
    # 쪽 배경·떠 있는 그림은 DocIR 이 버리므로 HWPX 원본에서 다시 읽는다(hwp_layers 주석).
    # 읽기에 실패해도 본문 렌더는 그대로 진행하고, 실패 사유를 manifest 에 남긴다.
    layers: dict[str, Any] = {}
    placements: list[dict[str, Any]] = []
    layer_error = None
    if os.environ.get("HWP_LAYERS", "on").strip().lower() not in {"off", "false", "0"}:
        try:
            layers = read_layers(hwpx if hwpx is not None else hwpx_bytes(path))
            placements = detach_floating_images(docir, layers) + page_fill_placements(docir, layers)
        except Exception as exc:  # noqa: BLE001 — 배경 복원 실패가 HWP 처리를 막지 않는다
            layer_error = f"{type(exc).__name__}: {exc}"
            layers, placements = {}, []
    rendered = render_review_html(
        document=DocumentInput(doc_ir=docir), annotations=[], title=path.stem,
    )
    if not rendered.ok:
        raise RuntimeError(f"HWP review HTML 생성 실패: {rendered.validation}")

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{path.stem}.review.html"
    first_page = next(iter(getattr(docir, "pages", None) or []), None)
    page_width_pt = float(getattr(first_page, "width_pt", None) or 595.28)
    page_height_pt = float(getattr(first_page, "height_pt", None) or 841.89)
    html, layer_records = inject_layers(rendered.html, docir, layers, placements)
    # 묶음 개체·글상자도 DocIR 이 버리므로 HWPX 원본에서 다시 놓는다(hwp_drawings 주석).
    drawing_records: list[dict[str, Any]] = []
    drawing_error = None
    if os.environ.get("HWP_DRAWINGS", "on").strip().lower() not in {"off", "false", "0"}:
        from .hwp_drawings import inject_drawings, read_drawings

        try:
            drawings = read_drawings(hwpx if hwpx is not None else hwpx_bytes(path))
            html, drawing_records = inject_drawings(html, docir, drawings)
        except Exception as exc:  # noqa: BLE001 — 글상자 복원 실패가 HWP 처리를 막지 않는다
            drawing_error = f"{type(exc).__name__}: {exc}"
    html_path.write_text(
        inject_review_surface_api(
            html,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
        ),
        encoding="utf-8",
    )
    manifest = {
        "source_file": path.name,
        "surface": "reconstructed_html",
        "coordinate_system": "browser_css_pixels_relative_to_document_page",
        "fidelity": "structural_reconstruction",
        "page_count": len(getattr(docir, "pages", None) or []) or 1,
        "page_css_size": [round(page_width_pt * 4 / 3, 2), round(page_height_pt * 4 / 3, 2)],
        "html": html_path.name,
        "bbox_api": "window.nhReviewSurface.boxes()",
        "highlight_api": "window.nhReviewSurface.highlight(nodeIds)",
        "limitations": [
            "원본 한컴 화면의 픽셀 동일 렌더가 아님",
            "쪽 배경과 용지·쪽 기준 떠 있는 그림, 글자가 있는 묶음 개체·글상자만 HWPX 원본 위치로 "
            "복원함(표 셀 안 그림·글 비켜 흐르기·양식 개체는 document-processor 지원 범위에 따름)",
            "사용자 화면과 bbox 판정에는 반드시 같은 HTML을 사용해야 함",
        ],
        # 복원한 겹: 종류(page_fill/floating_image)·쪽·용지 좌표(pt)·원본 감싸기 방식.
        "layers": layer_records,
        "layer_error": layer_error,
        # 원본 줄 위치로 더한 쪽 시작 문단과 보정 실패 사유.
        "pagination": pagination,
        # 되살린 묶음 개체·글상자: 쪽·놓은 곳(본문 흐름/쪽 겹)·크기(pt)·글상자 글자.
        "drawings": drawing_records,
        "drawing_error": drawing_error,
    }
    manifest_path = output_dir / f"{path.stem}.review.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return {**manifest, "html_path": str(html_path), "manifest_path": str(manifest_path)}


def read_dom_rows(html_path: Path, browser: str, *, timeout: int = 300) -> list[dict[str, Any]]:
    """headless Chromium이 실제 배치한 표 행과 셀 bbox를 읽는다."""
    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--dump-dom",
            html_path.resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()[-1500:]
        raise RuntimeError(f"HTML DOM bbox 추출 실패(exit={completed.returncode}): {detail}")
    match = re.search(
        r'<script[^>]+id=["\']nh-review-surface-data["\'][^>]*>(.*?)</script>',
        completed.stdout,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("HTML DOM bbox 결과를 찾을 수 없습니다")
    payload = json.loads(html_lib.unescape(match.group(1)))
    return list(payload.get("rows") or [])


# 문단 묶기. 표 밖 본문이나 큰 셀 안 본문은 HWP 문단(엔터) 하나가 대개 화면 한 줄이라
# 문단마다 Region 을 만들면 줄 단위가 되고(008 예금성 28개), 통째로 두면 페이지 하나가
# 된다(006 예금성 PaddleX 상자 1개). 심의 항목 단위로 묶는다: 빈 줄·표·항목 기호에서
# 새 묶음을 열고, 하위 기호(`-`·`*`·①)나 기호 없는 줄은 바로 위 항목에 잇는다.
_ITEM_START = re.compile(r"^\s*(?:[▶▷►□■◆◇●○◎◈★☆※☞✔❖]|\d{1,2}[.)]\s|[가-하][.)]\s)")


def _group_paragraph_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`domparagraph` 행들을 항목 단위 행으로 합친다. 나머지 행은 그대로 둔다."""
    others = [row for row in rows if not row.get("paragraph")]
    paragraphs = sorted(
        (row for row in rows if row.get("paragraph")),
        key=lambda row: (
            int(row.get("page_no") or 1), str(row["paragraph"].get("group")),
            int(row["paragraph"].get("index") or 0),
        ),
    )
    groups: list[list[dict[str, Any]]] = []
    previous = None
    for row in paragraphs:
        meta = row["paragraph"]
        key = (int(row.get("page_no") or 1), str(meta.get("group")))
        continues = (
            previous is not None
            and key == previous[0]
            and int(meta.get("index") or 0) == previous[1] + 1
            and groups and groups[-1]
        )
        previous = (key, int(meta.get("index") or 0))
        if meta.get("blank") or not str(row.get("text") or "").strip():
            groups.append([])  # 빈 줄은 묶음을 닫는다
            continue
        if not continues or _ITEM_START.match(str(row.get("text") or "")):
            groups.append([])
        groups[-1].append(row)
    merged = []
    for members in (group for group in groups if group):
        cells = [cell for member in members for cell in member.get("cells") or []]
        boxes = [member["bbox"] for member in members if member.get("bbox")]
        merged.append({
            "node_id": f"domparagraphs:{members[0]['node_id'].split(':', 1)[-1]}",
            "page_no": members[0].get("page_no"),
            "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                     max(b[2] for b in boxes), max(b[3] for b in boxes)],
            "text": "\n".join(str(member.get("text") or "") for member in members),
            # 한 묶음은 표가 아니라 문단 하나다. 문단 node_id 는 모두 유지한다.
            "cells": [{
                **cells[0], "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                     max(b[2] for b in boxes), max(b[3] for b in boxes)],
                "text": "\n".join(cell["text"] for cell in cells),
            }] if cells else [],
            "paragraph_node_ids": [cell.get("node_id") for cell in cells if cell.get("node_id")],
        })
    return others + merged


def dom_rows_to_blocks(
    rows: list[dict[str, Any]],
    *,
    page_no: int,
    canvas: tuple[int, int],
    page_css_size: tuple[float, float],
) -> list[dict[str, Any]]:
    """표시 HTML의 CSS 좌표를 파이프라인 캔버스 좌표의 행 Region으로 바꾼다."""
    rows = _group_paragraph_rows(rows)
    css_width, css_height = page_css_size
    scale_x = canvas[0] / max(css_width, 1.0)
    scale_y = canvas[1] / max(css_height, 1.0)

    def pixels(values: Any) -> list[int] | None:
        if not isinstance(values, list) or len(values) != 4:
            return None
        x0, y0, x1, y1 = (float(value) for value in values)
        box = [round(x0 * scale_x), round(y0 * scale_y), round(x1 * scale_x), round(y1 * scale_y)]
        box[0] = max(0, min(canvas[0], box[0]))
        box[2] = max(0, min(canvas[0], box[2]))
        box[1] = max(0, min(canvas[1], box[1]))
        box[3] = max(0, min(canvas[1], box[3]))
        return box if box[2] > box[0] and box[3] > box[1] else None

    blocks: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("page_no") or 1) != page_no:
            continue
        bbox = pixels(row.get("bbox"))
        cells = []
        for cell in row.get("cells") or []:
            text = str(cell.get("text") or "").strip()
            cell_bbox = pixels(cell.get("bbox"))
            if not text or cell_bbox is None:
                continue
            cells.append({
                "row": 0,
                "col": 0,
                "row_span": max(1, int(cell.get("row_span") or 1)),
                "col_span": 1,
                "is_header": len(cells) == 0 and len(row.get("cells") or []) > 1,
                "text": text,
                "bbox": cell_bbox,
                "node_id": str(cell.get("node_id") or ""),
            })
        # 중첩 표는 각 계층의 col 번호가 다시 0/1부터 시작해 서로 충돌한다.
        # 한 화면 행의 P3 표현에서는 실제 x 순서가 정답이므로, 보이는 셀을 왼쪽부터
        # 연속 열로 다시 번호 매긴다. 행 머리글과 내부 값 셀이 같은 칸을 덮지 않는다.
        cells.sort(key=lambda item: (item["bbox"][0], item["bbox"][1]))
        for col, cell in enumerate(cells):
            cell["col"] = col
            cell["is_header"] = col == 0 and len(cells) > 1
        text = "\n".join(cell["text"] for cell in cells)
        if bbox is None or not text:
            continue
        is_table = len(cells) >= 2
        block: dict[str, Any] = {
            "bbox": bbox,
            "label": "table" if is_table else "text",
            "kind": "table" if is_table else "text",
            "content": text,
            "text_source": "document_processor_html",
            "bbox_source": "reconstructed_html_dom",
            "bbox_quality": "display_exact",
            "structured": {
                "source": "document_processor_html",
                "node_kind": "dom_paragraph_group" if row.get("paragraph_node_ids") else "dom_table_row",
                "node_ids": list(row.get("paragraph_node_ids") or [])
                or [cell["node_id"] for cell in cells if cell["node_id"]],
                "surface_node_id": str(row.get("node_id") or ""),
            },
        }
        if is_table:
            cols = len(cells)
            block["table"] = {
                "source": "document_processor",
                "grid": {"rows": 1, "cols": max(1, cols)},
                "cells": cells,
                "notes": [],
            }
        blocks.append(block)
    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    for order, block in enumerate(blocks, start=1):
        block["order"] = order
        block["piece"] = 0
    return blocks
