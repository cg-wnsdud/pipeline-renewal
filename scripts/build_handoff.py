"""저장된 의미 단계 결과(layout-result/3)로 심의 단계 입력(review-input/2)만 다시 만든다(모델 호출 없음, 기존 결과 파일은 읽기만).

예:
  .venv/Scripts/python.exe scripts/build_handoff.py --semantic work/pipeline/<실행 폴더>/semantic
  # run_semantic.py 단독 출력 폴더도 같다
  .venv/Scripts/python.exe scripts/build_handoff.py --semantic work/semantic/<폴더>

출력: <semantic>/handoff/<submission_id>.json, <semantic>/handoff/check.json(참조·값 보존 검사)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import handoff  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semantic", type=Path, required=True, action="append",
                    help="files/index.json 이 있는 의미 단계 출력 폴더(여러 번 지정 가능)")
    args = ap.parse_args()
    out = {str(d): handoff.write_all(d) for d in args.semantic}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if not any(v["problems"] for r in out.values() for v in r.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
