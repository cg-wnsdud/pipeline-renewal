"""원본 미리보기(docs/21): 원본 PDF·HWP·HWPX 쪽을 파싱과 같은 방식으로 그린다(OCR·모델 호출 없음).

등록 직후(파싱 전) 화면의 원본 미리보기와 파싱 결과의 근거 박스가 같은 그림 위에 놓이도록, 파싱(stage1)과 같은 렌더를 쓴다.
- PDF: pages.render_pdf_page 로 PDF_DPI(200, run_pipeline 기본) 렌더. 스캔 PDF 도 같은 DPI 다(기존 parser-pipeline 은 스캔 쪽을
  내장 해상도로 그렸다).
- HWP·HWPX: hwp_bridge 변환(쪽을 덮는 배경 레이어가 있으면 레이어 없이 다시 변환, docs/19) → 변환 PDF 를 같은 방식으로 렌더.
  JVM·Chromium 을 띄우므로 별도 프로세스로 실행한다: `python -m layout_pipeline.preview <원본> <출력 폴더>` → page-0001.png …
- 이미지: 그리지 않는다. 파싱은 EXIF 방향을 반영하므로 방향 정보가 있는 사진은 원본 그대로 보이는 그림과 다를 수 있다.
"""
from __future__ import annotations

import argparse
import io
import sys
import tempfile
from pathlib import Path

from PIL import Image

PDF_DPI = 200


def _pdf(source):
    import pypdfium2 as pdfium
    return pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))


def pdf_page_count(source: bytes | str | Path) -> int:
    pdf = _pdf(source)
    try:
        return len(pdf)
    finally:
        pdf.close()


def render_pdf(source: bytes | str | Path, page_no: int | None = None, dpi: int = PDF_DPI) -> tuple[list[Image.Image], int]:
    """(쪽 이미지 목록, 전체 쪽 수). page_no 가 없으면 모든 쪽, 범위 밖이면 IndexError."""
    from .pages import render_pdf_page
    pdf = _pdf(source)
    try:
        total = len(pdf)
        if page_no is not None and not 1 <= page_no <= total:
            raise IndexError(f"쪽 번호 범위 밖: {page_no}/{total}")
        images = []
        for n in ([page_no] if page_no is not None else range(1, total + 1)):
            page = pdf[n - 1]
            try:
                images.append(render_pdf_page(page, dpi))
            finally:
                page.close()
        return images, total
    finally:
        pdf.close()


def png_bytes(image: Image.Image) -> bytes:
    """파싱 페이지 저장(pages._save)과 같은 PNG 인코딩."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def render_hwp(source: Path, work: Path, dpi: int = PDF_DPI) -> list[Image.Image]:
    from .config import get_settings
    from .hwp_bridge import convert_hwp_to_pdf
    settings = get_settings()
    pdf = work / "converted.pdf"
    convert_hwp_to_pdf(source, pdf, python=settings.hwp_python, project=settings.hwp_project)
    return render_pdf(pdf, None, dpi)[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="원본 HWP·HWPX·PDF 의 모든 쪽을 파싱과 같은 방식으로 그린다(page-0001.png …)")
    ap.add_argument("source", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lp-preview-") as tmp:
        if args.source.suffix.lower() in (".hwp", ".hwpx"):
            images = render_hwp(args.source, Path(tmp))
        else:
            images = render_pdf(args.source)[0]
    for i, im in enumerate(images, 1):
        (args.out / f"page-{i:04d}.png").write_bytes(png_bytes(im))
    print(len(images))
    return 0


if __name__ == "__main__":
    sys.exit(main())
