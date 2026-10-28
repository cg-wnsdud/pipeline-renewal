"""HWP/HWPX → PDF: 기존 parser-pipeline 의 변환 경로만 별도 프로세스로 호출한다.

출처: new-pipeline/parser_next/hwp_bridge.py 를 그대로 옮긴 변환 연결 어댑터(2026-10-24).
이 저장소에서 기존 코드로부터 가져온 것은 이 파일뿐이다. 파싱 알고리즘은 가져오지 않았다.

재사용 범위는 `nh_parser_fin.ingest.hwp_render.render_hwp_to_pdf(path, output)` 하나다.
기존 원본 미리보기(`render_hwp_preview`)와 같은 호출이며, 기존 구조 파싱·DOM 행·triage·
OCR 보정·라벨링 결과는 사용하지 않는다. 변환된 PDF 는 이 파서의 PDF 경로와 같은 방식으로
렌더한다. 기존 변환기는 JVM/Chromium 을 띄우므로 별도 프로세스·별도 가상환경에서 실행한다.

Windows 경로 주의: 기존 변환기의 JPype 는 JVM 시작 시 비ASCII 경로(예: '농협프로젝트')의
네이티브 라이브러리를 읽지 못한다(2026-10-24 확인: UnsatisfiedLinkError, 경로가 한글 앞에서 잘림).
그래서 변환 중에만 ASCII 임시 폴더에 디렉터리 정션을 만들어 기존 가상환경을 그 경로로 실행하고,
입력·출력 파일도 ASCII 임시 경로를 거친다. 끝나면 정션 링크만 제거한다(대상 폴더는 건드리지 않음).
기존 코드와 시스템 설정은 바꾸지 않는다.

쪽 배경 그림(docs/19): 기존 변환기는 '글 앞으로'(IN_FRONT_OF_TEXT) 떠 있는 그림을 본문 위(z-index 2)에 그린다. 쪽 대부분을 덮는
그런 그림이 있으면 본문이 가려져 렌더 이미지에 글자가 없다(whole-test 대출 광고2.hwp 1쪽). 그런 레이어가 있으면 기존 변환기의
설정 HWP_LAYERS=off(쪽 배경 레이어를 따로 복원하지 않음)로 한 번 더 변환하고, 그 사실을 변환 정보(layers_disabled)에 남긴다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_MARKER = "@@PN_HWP_BRIDGE@@"

_SCRIPT = "\n".join([
    "import json, sys",
    "from pathlib import Path",
    "from nh_parser_fin.ingest.hwp_render import render_hwp_to_pdf",
    "info = render_hwp_to_pdf(Path(sys.argv[1]), Path(sys.argv[2]))",
    "surface = info.get('review_surface') or {}",
    "slim = {k: v for k, v in info.items() if k != 'review_surface'}",
    "keep = ('surface', 'fidelity', 'page_count', 'page_css_size', 'limitations',",
    "        'layer_error', 'drawing_error', 'pagination')",
    "slim['review_surface'] = {k: surface.get(k) for k in keep if k in surface}",
    "slim['review_surface']['layers'] = len(surface.get('layers') or [])",
    "slim['review_surface']['layer_items'] = [{k: l.get(k) for k in ('kind', 'page_no', 'wrap', 'box_pt')}",
    "                                         for l in surface.get('layers') or []]",
    "slim['review_surface']['drawings'] = len(surface.get('drawings') or [])",
    "print(sys.argv[3] + json.dumps(slim, ensure_ascii=False, default=str))",
])


class HwpBridgeError(RuntimeError):
    """한글 변환 실패. 입력 오류로 전달하며 다른 경로로 대체하지 않는다."""


def _is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def _make_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                               text=True, encoding="mbcs", errors="replace", check=False)
    if completed.returncode != 0 or not link.exists():
        raise HwpBridgeError(f"ASCII 정션 생성 실패: {completed.stdout} {completed.stderr}")


def _remove_junction(link: Path) -> None:
    if link.exists() or os.path.islink(link):
        os.rmdir(link)   # 정션 링크만 제거한다. 대상 폴더 내용은 지우지 않는다.


# 쪽 면적의 이 비율 이상을 덮는 '글 앞으로' 레이어가 있으면 본문이 가려진 것으로 보고 레이어 없이 다시 변환한다.
FRONT_LAYER_COVER_MIN = 0.6
FRONT_WRAPS = {"IN_FRONT_OF_TEXT"}
CSS_PX_TO_PT = 0.75


def covering_layers(info: dict) -> list[dict]:
    """본문을 가리는 쪽 레이어: wrap 이 '글 앞으로'이고 쪽 면적의 FRONT_LAYER_COVER_MIN 이상을 덮는 것."""
    surface = info.get("review_surface") or {}
    size = surface.get("page_css_size")
    if not size:
        return []
    pw, ph = size[0] * CSS_PX_TO_PT, size[1] * CSS_PX_TO_PT
    out = []
    for item in surface.get("layer_items") or []:
        box = item.get("box_pt")
        if item.get("wrap") not in FRONT_WRAPS or not box:
            continue
        w = max(0.0, min(box[2], pw) - max(box[0], 0.0))
        h = max(0.0, min(box[3], ph) - max(box[1], 0.0))
        cover = w * h / (pw * ph)
        if cover >= FRONT_LAYER_COVER_MIN:
            out.append(item | {"cover": round(cover, 3)})
    return out


def convert_hwp_to_pdf(source: Path, output_pdf: Path, *, python: str | None,
                       project: str | None, timeout_s: int = 900) -> dict:
    info = _convert(source, output_pdf, python=python, project=project, timeout_s=timeout_s)
    covering = covering_layers(info)
    if not covering:
        return info
    first = {"pdf_bytes": info.get("pdf_bytes"), "bridge_seconds": info.get("bridge_seconds"), "covering_layers": covering}
    again = _convert(source, output_pdf, python=python, project=project, timeout_s=timeout_s, env_extra={"HWP_LAYERS": "off"})
    again["layers_disabled"] = {"reason": "front_layer_covers_page", "env": {"HWP_LAYERS": "off"}, "first_conversion": first}
    return again


def _convert(source: Path, output_pdf: Path, *, python: str | None, project: str | None, timeout_s: int,
             env_extra: dict | None = None) -> dict:
    if not python or not project:
        raise HwpBridgeError("PN_HWP_PYTHON / PN_HWP_PROJECT 가 설정되지 않았습니다")
    python_path, project_path = Path(python), Path(project)
    if not python_path.is_file():
        raise HwpBridgeError(f"기존 변환기 python 을 찾을 수 없습니다: {python}")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    if output_pdf.exists():
        output_pdf.unlink()
    temp_root = Path(tempfile.gettempdir())
    if not _is_ascii(str(temp_root)):
        raise HwpBridgeError(f"ASCII 임시 폴더가 필요합니다: {temp_root}")
    work = Path(tempfile.mkdtemp(prefix="pn-hwp-", dir=temp_root))
    link = work / "legacy"
    started = time.perf_counter()
    try:
        run_project, run_python = project_path, python_path
        junction = not (_is_ascii(str(project_path)) and _is_ascii(str(python_path)))
        if junction:
            try:
                relative = python_path.resolve().relative_to(project_path.resolve())
            except ValueError as exc:
                raise HwpBridgeError("PN_HWP_PYTHON 은 PN_HWP_PROJECT 아래의 가상환경이어야 합니다") from exc
            _make_junction(link, project_path.resolve())
            run_project, run_python = link, link / relative
        staged_in = work / ("input" + source.suffix.lower())
        staged_out = work / "output.pdf"
        shutil.copy2(source, staged_in)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["HWP_REVIEW_DIR"] = str(work / "review-html")
        env.update(env_extra or {})
        completed = subprocess.run(
            [str(run_python), "-c", _SCRIPT, str(staged_in), str(staged_out), _MARKER],
            cwd=str(run_project), env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, check=False,
        )
        seconds = round(time.perf_counter() - started, 3)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            raise HwpBridgeError(f"HWP 변환 실패(exit={completed.returncode}): {detail}")
        info = None
        for line in completed.stdout.splitlines():
            if line.startswith(_MARKER):
                info = json.loads(line[len(_MARKER):])
        if info is None:
            raise HwpBridgeError("변환기 결과 정보를 읽지 못했습니다: " + completed.stdout[-500:])
        if not staged_out.is_file() or staged_out.read_bytes()[:5] != b"%PDF-":
            raise HwpBridgeError("변환은 끝났지만 유효한 PDF 가 없습니다")
        shutil.copy2(staged_out, output_pdf)
        review = work / "review-html"
        if review.is_dir():
            shutil.copytree(review, output_pdf.parent / f"{output_pdf.stem}-review-html", dirs_exist_ok=True)
        info.update(bridge_seconds=seconds, converter="nh_parser_fin.ingest.hwp_render.render_hwp_to_pdf",
                    ascii_junction=junction, source_name=source.name)
        return info
    finally:
        try:
            _remove_junction(link)
        except OSError:
            pass
        # 정션이 남아 있으면 임시 폴더를 지우지 않는다(링크 대상 보호). 남은 폴더는 수동 정리 대상.
        if not (link.exists() or os.path.islink(link)):
            shutil.rmtree(work, ignore_errors=True)
