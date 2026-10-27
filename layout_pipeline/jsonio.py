"""UTF-8 JSON 입출력과 해시."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def dump(path: Path, value: Any, *, indent: int | None = 1) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=indent, default=str)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        # 모델 원문에 짝 없는 서로게이트가 있으면 값은 그대로 두고 ASCII 이스케이프로 저장한다.
        data = json.dumps(value, ensure_ascii=True, indent=indent, default=str).encode("ascii")
    path.write_bytes(data)
    return path


def load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
