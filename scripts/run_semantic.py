"""의미 단계만 실행: 저장된 파싱 결과(layout-result/2)로 상품·템플릿·영역 소속 결정(observe) → 라벨링 → layout-result/3·화면.

OCR·전사(판독)는 호출하지 않는다. 파싱 폴더는 읽기만 하고 결과는 새 출력 폴더에 쓴다(기존 snapshot·이전 실행을 덮어쓰지 않음).

예:
  # 파싱 실행 폴더(run_pipeline --out)로 분류·라벨링만 다시
  .venv/Scripts/python.exe scripts/run_semantic.py --parse work/pipeline/<실행 폴더> --out work/semantic/<새 폴더>
  # 일부 묶음만
  .venv/Scripts/python.exe scripts/run_semantic.py --parse work/pipeline/<실행 폴더> --submissions tt02,tt03 --out work/semantic/x
  # 저장된 의미 단계 응답으로 결과·화면 재구성(모델 호출 없음)
  .venv/Scripts/python.exe scripts/run_semantic.py --rebuild --out work/semantic/<기존 폴더>
  # 요청만 만들어 보기(호출 없음): 별칭·프롬프트 크기·관측 단위 확인
  .venv/Scripts/python.exe scripts/run_semantic.py --parse <파싱 폴더> --dry-run

출력(<out>/):
  files/<file_id>/result.json   layout-result/3(파일별)
  files/index.json·index.html   목록·대조 화면(상품·템플릿·배정·라벨)
  report.html                   입력 전체 검토 화면(파일별 상품·라벨·검토·소요시간)
  files/contract-check.json     계약·참조·보존·인용 위치 검사
  trace/semantic/calls/*.json   요청 메타(프롬프트·스키마·이미지 해시)와 원시 응답
  trace/semantic/<묶음>.json     별칭·관측·통합·근거 인용·특징 ID·라벨 근거·버린 라벨
  report.json                   묶음별 상품·템플릿·라벨 분포·호출 수·시간·검사 요약(--rebuild 는 report.rebuild.json)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import classify, semantic  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402


def dry_run(parse_dir: Path, submissions: set[str] | None, classification_mode: str = "observe") -> dict:
    docs, paths = semantic.load_parse(parse_dir)
    by_sub: dict[str, list[dict]] = {}
    for d in docs:
        by_sub.setdefault(d["submission_id"], []).append(d)
    out = {}
    for sub, ds in by_sub.items():
        if submissions and sub not in submissions:
            continue
        b = classify.build_bundle(ds, paths)
        plan = classify.plan_units(b)
        reqs = [classify.observe_request(b, u, len(plan) == 1) for u in plan]
        out[sub] = {"pages": len(b["page_order"]), "regions": len(b["regions"]), "units": [u for u in plan],
                    "classification_mode": classification_mode,
                    "prompt_chars": [len(r["text"]) for r in reqs], "images": [len(r["images"]) for r in reqs]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parse", type=Path, help="파싱 실행(또는 내보내기) 폴더: files/index.json 과 layout-result/2 문서가 있는 곳")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--submissions", default="", help="쉼표로 구분한 submission_id 만")
    ap.add_argument("--classification-mode", choices=list(semantic.CLASSIFICATION_MODES), default="observe",
                    help="observe(기본): 관측 한 호출에서 상품·템플릿·모든 영역 소속을 함께 결정 + 이름 없는 대상 정리. "
                         "legacy: 정리 규칙 없는 이전 관측·통합(이전 실행 재구성용)")
    ap.add_argument("--vlm-concurrency", type=int, default=2, help="GPU 동시 호출 상한(파싱 단계 기본값과 같은 2). 의미 단계 전체가 공유")
    ap.add_argument("--rebuild", action="store_true", help="<out> 의 저장 응답으로 결과·화면 재구성(호출 없음)")
    ap.add_argument("--dry-run", action="store_true", help="요청만 만들어 관측 단위·프롬프트 크기를 보인다(호출 없음)")
    args = ap.parse_args()
    subs = {s for s in args.submissions.split(",") if s} or None
    if args.dry_run:
        print(json.dumps(dry_run(args.parse, subs, args.classification_mode), ensure_ascii=False, indent=1))
        return 0
    if not args.out:
        ap.error("--out 이 필요합니다")
    if args.rebuild:
        report = semantic.rebuild(args.out)
    else:
        if not args.parse:
            ap.error("--parse 가 필요합니다")
        if args.out.exists() and any(args.out.iterdir()):
            ap.error(f"출력 폴더가 비어 있지 않습니다(이전 결과 덮어쓰기 방지): {args.out}")
        load_env_file()
        client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                           prefix="sem")
        report = semantic.run(args.parse, args.out, client, args.vlm_concurrency, subs, classification_mode=args.classification_mode)
    print(json.dumps({"checks": report["checks"], "timing": report["timing"], "viewer": report["viewer"], "report_html": report["report_html"],
                      "submissions": [{k: s[k] for k in ("submission_id", "mode", "status", "products", "labeling")}
                                      for s in report["submissions"]]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
