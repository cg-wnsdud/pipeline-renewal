"""HWP/HWPX를 기존 PDF 파이프라인에 연결하는 로컬 렌더 어댑터."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _find_chromium() -> str | None:
    configured = os.environ.get("HWP_CHROMIUM", "").strip()
    if configured:
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    for candidate in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _render_document_processor_html(path: Path, output: Path, *, prepared=None) -> dict:
    from .hwp_html import read_dom_rows, render_hwp_review_surface

    browser = _find_chromium()
    if not browser:
        raise RuntimeError("headless Chromium/Chrome/Edge 실행 파일을 찾을 수 없습니다")
    configured = os.environ.get("HWP_REVIEW_DIR", "").strip()
    review_dir = Path(configured) if configured else output.parent / "review-html"
    surface = render_hwp_review_surface(path, review_dir, prepared=prepared)
    html_path = Path(surface["html_path"]).resolve()
    timeout = int(os.environ.get("HWP_RENDER_TIMEOUT", "300"))
    surface["dom_rows"] = read_dom_rows(html_path, browser, timeout=timeout)
    Path(surface["manifest_path"]).write_text(
        json.dumps(
            {key: value for key, value in surface.items() if key != "html_path"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={output.resolve()}",
            html_path.as_uri(),
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
        raise RuntimeError(f"HTML→PDF 렌더 실패(exit={completed.returncode}): {detail}")
    return {
        "engine": "document_processor_html_chromium",
        "transport": "local_process",
        "review_surface": surface,
    }


def _find_soffice() -> str | None:
    configured = os.environ.get("HWP_SOFFICE", "").strip()
    if configured:
        return configured
    for name in ("soffice", "libreoffice"):
        if found := shutil.which(name):
            return found
    for candidate in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _render_libreoffice(path: Path, output: Path) -> dict:
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice soffice 실행 파일을 찾을 수 없습니다")
    timeout = int(os.environ.get("HWP_RENDER_TIMEOUT", "300"))
    with tempfile.TemporaryDirectory(prefix="nh-hwp-libreoffice-") as directory:
        converted = Path(directory) / f"{path.stem}.pdf"
        completed = subprocess.run(
            [
                soffice,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                directory,
                str(path.resolve()),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0 or not converted.is_file():
            detail = (completed.stderr or completed.stdout or "출력 파일 없음").strip()[-1500:]
            raise RuntimeError(f"LibreOffice HWP→PDF 실패(exit={completed.returncode}): {detail}")
        shutil.copy2(converted, output)
    return {
        "engine": "libreoffice_writer_pdf",
        "transport": "local_process",
    }


def backend_order(requested: str | None = None) -> list[str]:
    value = (requested or os.environ.get("HWP_RENDER_BACKEND", "auto")).strip().lower()
    aliases = {"html": "document_processor_html", "document-processor-html": "document_processor_html"}
    value = aliases.get(value, value)
    allowed = {"auto", "document_processor_html", "libreoffice"}
    if value not in allowed:
        raise RuntimeError(f"지원하지 않는 HWP_RENDER_BACKEND: {value}")
    if value != "auto":
        return [value]
    return ["document_processor_html", "libreoffice"]


def _validate_pdf(output: Path) -> None:
    deadline = time.monotonic() + 30
    previous = -1
    stable = 0
    while time.monotonic() < deadline:
        size = output.stat().st_size if output.exists() else 0
        if size > 1000 and size == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = size
        time.sleep(0.5)
    if not output.exists() or output.stat().st_size <= 1000 or output.read_bytes()[:5] != b"%PDF-":
        raise RuntimeError("렌더 프로세스는 끝났지만 유효한 PDF가 생성되지 않았습니다")


def render_hwp_to_pdf(path: Path, output: Path, *, prepared=None) -> dict:
    """환경에 맞는 로컬 렌더러를 선택해 HWP/HWPX를 PDF로 만든다.

    ``auto``는 운영체제와 무관하게 구조 HTML과 headless Chromium을 먼저
    사용한다. LibreOffice는 호환되는 문서를 위한 폴백이다. 렌더 단계에서는
    문서를 외부 서버로 전송하지 않는다.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    renderers = {
        "document_processor_html": _render_document_processor_html,
        "libreoffice": _render_libreoffice,
    }
    order = backend_order()
    for backend in order:
        if output.exists():
            output.unlink()
        try:
            if backend == "document_processor_html" and prepared is not None:
                info = renderers[backend](path, output, prepared=prepared)
            else:
                info = renderers[backend](path, output)
            _validate_pdf(output)
            return {
                **info,
                "backend": backend,
                "attempted_backends": [*order[: order.index(backend) + 1]],
                "pdf_bytes": output.stat().st_size,
            }
        except Exception as exc:
            errors.append(f"{backend}: {exc}")
            if len(order) == 1:
                raise
    raise RuntimeError("HWP 렌더러가 모두 실패했습니다: " + " | ".join(errors))
