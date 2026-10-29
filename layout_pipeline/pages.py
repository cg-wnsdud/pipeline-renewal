"""입력 파일 → 페이지 이미지(표시 좌표계 = OCR 좌표계의 기준).

- 이미지: EXIF 방향을 반영한 뒤 크기를 바꾸지 않는다. 투명 배경은 흰색으로 합성한다.
- PDF: 지정 DPI 로 모든 페이지를 렌더한다. 디지털 텍스트로 별도 파싱 경로를 만들지 않는다.
- HWP/HWPX: hwp_bridge 가 만든 PDF 를 PDF 와 같은 방식으로 렌더한다.

여러 파일·페이지를 붙이거나 자르지 않는다. 파일 순서 → 페이지 순서를 그대로 보존한다.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from .hwp_bridge import convert_hwp_to_pdf
from .jsonio import dump, load, sha256_file

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
PDF_SUFFIXES = {".pdf"}
HWP_SUFFIXES = {".hwp", ".hwpx"}


class InputError(RuntimeError):
    """입력을 페이지 이미지로 만들 수 없음. 빈 결과로 위장하지 않는다."""


@dataclass(frozen=True)
class InputFile:
    file_id: str
    path: Path

    @property
    def kind(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return "image"
        if suffix in PDF_SUFFIXES:
            return "pdf"
        if suffix in HWP_SUFFIXES:
            return suffix[1:]
        return "unsupported"


@dataclass(frozen=True)
class Submission:
    submission_id: str
    files: tuple[InputFile, ...]
    description: str = ""


@dataclass
class Page:
    page_id: str
    submission_id: str
    file_id: str
    page_no: int
    width: int
    height: int
    image_path: str
    image_sha256: str
    source: dict = field(default_factory=dict)

    @property
    def aspect(self) -> float:
        return max(self.width, self.height) / min(self.width, self.height)

    @property
    def orientation(self) -> str:
        if self.width == self.height:
            return "square"
        return "portrait" if self.height > self.width else "landscape"

    def geometry(self) -> dict:
        return {"width": self.width, "height": self.height, "aspect": round(self.aspect, 4),
                "orientation": self.orientation, "pixels": self.width * self.height}

    def to_dict(self) -> dict:
        return asdict(self) | {"geometry": self.geometry()}

    def open_image(self) -> Image.Image:
        with Image.open(self.image_path) as handle:
            return handle.convert("RGB")


def load_manifest(path: Path) -> list[Submission]:
    data = load(path)
    root = Path(os.environ.get("LP_FIXTURE_ROOT") or data["source_root"])
    seen: set[str] = set()
    out = []
    for entry in data["submissions"]:
        files = []
        for item in entry["files"]:
            if item["file_id"] in seen:
                raise InputError(f"중복 file_id: {item['file_id']}")
            seen.add(item["file_id"])
            raw = Path(item["path"])
            files.append(InputFile(item["file_id"], raw if raw.is_absolute() else root / raw))
        out.append(Submission(entry["submission_id"], tuple(files), entry.get("description", "")))
    return out


def _flatten(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _save(image: Image.Image, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return sha256_file(path)


def _image_pages(sub: Submission, item: InputFile, out_dir: Path, source_sha: str) -> list[Page]:
    with Image.open(item.path) as handle:
        exif = handle.getexif().get(0x0112)
        stored = list(handle.size)
        fmt = handle.format
        image = _flatten(ImageOps.exif_transpose(handle))
    page_id = f"{item.file_id}_p01"
    out = out_dir / f"{page_id}.png"
    return [Page(page_id, sub.submission_id, item.file_id, 1, image.width, image.height, str(out), _save(image, out), {
        "kind": "image", "format": fmt, "source_name": item.path.name, "source_sha256": source_sha,
        "stored_size": stored, "exif_orientation": exif, "exif_transposed": exif not in (None, 1),
    })]


def _pdf_pages(sub: Submission, item: InputFile, pdf_path: Path, out_dir: Path, dpi: int, kind: str,
               extra: dict, source_sha: str) -> list[Page]:
    import pypdfium2 as pdfium

    pages = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(pdf)
        for index in range(total):
            page = pdf[index]
            try:
                size_pt = [round(v, 3) for v in page.get_size()]
                rotation = page.get_rotation()
                image = page.render(scale=dpi / 72.0, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
            finally:
                page.close()
            page_id = f"{item.file_id}_p{index + 1:02d}"
            out = out_dir / f"{page_id}.png"
            pages.append(Page(page_id, sub.submission_id, item.file_id, index + 1, image.width, image.height,
                              str(out), _save(image, out), {
                                  "kind": kind, "source_name": item.path.name, "source_sha256": source_sha,
                                  "renderer": "pypdfium2", "dpi": dpi, "page_pt": size_pt,
                                  "pdf_rotation": rotation, "page_count": total, **extra}))
    finally:
        pdf.close()
    return pages


def _hwp_pdf(item: InputFile, out_dir: Path, source_sha: str, hwp_python: str | None,
             hwp_project: str | None) -> tuple[Path, dict]:
    """변환 PDF 는 원본 해시가 같을 때만 재사용한다(JVM/Chromium 변환 비용).
    변환기의 DOM 행(문단·표 행 문구와 화면 좌표)은 <file_id>.dom_rows.json 에 따로 두고 경로만 남긴다(hwp_structure, docs/20).
    DOM 행 기록이 없는 이전 변환은 다시 변환한다."""
    pdf_path = out_dir / "hwp" / f"{item.file_id}.pdf"
    meta_path = pdf_path.with_suffix(".conversion.json")
    rows_path = pdf_path.with_suffix(".dom_rows.json")
    if pdf_path.is_file() and meta_path.is_file() and rows_path.is_file():
        meta = load(meta_path)
        if meta.get("source_sha256") == source_sha and meta.get("dom_rows"):
            return pdf_path, meta | {"reused_conversion": True}
    info = convert_hwp_to_pdf(item.path, pdf_path, python=hwp_python, project=hwp_project)
    rows = info.pop("dom_rows", None) or []
    dump(rows_path, rows)
    meta = {"source_sha256": source_sha, "conversion": info,
            "dom_rows": {"path": str(rows_path), "rows": len(rows),
                         "page_css_size": (info.get("review_surface") or {}).get("page_css_size")}}
    dump(meta_path, meta)
    return pdf_path, meta | {"reused_conversion": False}


def render_submission(sub: Submission, out_dir: Path, *, pdf_dpi: int, hwp_python: str | None = None,
                      hwp_project: str | None = None) -> list[Page]:
    pages: list[Page] = []
    for item in sub.files:
        if not item.path.is_file():
            raise InputError(f"입력 파일이 없습니다: {item.path}")
        source_sha = sha256_file(item.path)
        if item.kind == "image":
            pages += _image_pages(sub, item, out_dir, source_sha)
        elif item.kind == "pdf":
            pages += _pdf_pages(sub, item, item.path, out_dir, pdf_dpi, "pdf", {}, source_sha)
        elif item.kind in ("hwp", "hwpx"):
            pdf_path, meta = _hwp_pdf(item, out_dir, source_sha, hwp_python, hwp_project)
            pages += _pdf_pages(sub, item, pdf_path, out_dir, pdf_dpi, item.kind,
                                {"converted_pdf": str(pdf_path), "conversion": meta}, source_sha)
        else:
            raise InputError(f"지원하지 않는 형식입니다: {item.path.name}")
    if not pages:
        raise InputError(f"페이지가 없습니다: {sub.submission_id}")
    return pages
