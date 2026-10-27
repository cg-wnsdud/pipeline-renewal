"""환경 설정. 접속 정보는 .env/환경변수로만 주입하고 산출물에 URL 을 남기지 않는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> None:
    """.env 값은 아직 설정되지 않은 환경변수에만 채운다."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    ocr_url: str | None
    ocr_timeout_s: float
    hwp_python: str | None
    hwp_project: str | None
    work_dir: Path


def get_settings() -> Settings:
    load_env_file()
    return Settings(
        ocr_url=os.environ.get("PN_OCR_URL", "").strip() or None,
        ocr_timeout_s=float(os.environ.get("PN_OCR_TIMEOUT_S", "180")),
        hwp_python=os.environ.get("PN_HWP_PYTHON", "").strip() or None,
        hwp_project=os.environ.get("PN_HWP_PROJECT", "").strip() or None,
        work_dir=Path(os.environ.get("LP_WORK_DIR", "").strip() or PROJECT_ROOT / "work"),
    )
