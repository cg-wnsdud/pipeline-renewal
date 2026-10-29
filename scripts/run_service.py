"""파서 서비스 실행(docs/21): 요청 JSON 하나 → run_pipeline 기본 프로필 실행 → <out>/service.json. 종료 코드로 상태를 알린다.

nh-ad-compliance 파서 서비스(apps/parser/app.py)가 요청마다 이 스크립트를 별도 프로세스로 실행하고 <out> 을 ZIP 으로 돌려준다
(PDFium 이 스레드 안전하지 않고 HWP 변환기가 프로세스마다 JVM 을 띄우므로 실행을 프로세스로 나눈다). 요청·service.json 형식과
상태 판정은 layout_pipeline/service.py.

예:
  .venv/Scripts/python.exe scripts/run_service.py --request work/service/req.json --out work/service/<새 폴더>

종료 코드: 0 완료·부분 완료, 1 내부 오류, 2 입력 오류, 3 처리 못 한 쪽·모델 호출 실패(다시 시도 가능), 4 계약 검사 실패.
manifest 는 <out> 옆(<out>.manifest.json)에 쓴다(<out> 은 실행 결과만 담는다).
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_pipeline  # noqa: E402

from layout_pipeline import service  # noqa: E402
from layout_pipeline.hwp_bridge import HwpBridgeError  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.pages import InputError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", type=Path, required=True, help="요청 JSON(layout_pipeline/service.py)")
    ap.add_argument("--out", type=Path, required=True, help="실행 폴더(비어 있거나 없어야 함)")
    ap.add_argument("--vlm-concurrency", type=int, default=2, help="VLM 동시 호출 상한(run_pipeline 기본값과 같은 2)")
    args = ap.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        ap.error(f"출력 폴더가 비어 있지 않습니다: {args.out}")
    req = None
    try:
        req = service.validate(load(args.request))
        manifest = service.write_manifest(req, args.out.parent / f"{args.out.name}.manifest.json")
        run_pipeline.main(["--manifest", str(manifest), "--out", str(args.out), "--vlm-concurrency", str(args.vlm_concurrency)])
        doc = service.assess(args.out, req)
    except (service.RequestError, InputError, HwpBridgeError, json.JSONDecodeError) as exc:
        doc = service.failure_doc(req, "INPUT_INVALID", f"{type(exc).__name__}: {exc}")
    except Exception:                                   # noqa: BLE001 — 결과를 판정할 수 없는 실행 오류
        doc = service.failure_doc(req, "INTERNAL_ERROR", traceback.format_exc()[-2000:])
    dump(args.out / "service.json", doc)
    print(json.dumps({k: doc.get(k) for k in ("status", "failure", "review_input")}, ensure_ascii=False))
    return service.exit_code(doc)


if __name__ == "__main__":
    raise SystemExit(main())
