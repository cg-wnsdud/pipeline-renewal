"""실행 폴더의 기록으로 대조 화면만 다시 만든다(모델 호출 없음). 사용: make_viewer.py <run_dir>"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.viewer import build_viewer  # noqa: E402

if __name__ == "__main__":
    print(build_viewer(Path(sys.argv[1])))
