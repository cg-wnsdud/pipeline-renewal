"""Stage 1 실행: 입력 → 경로 → OCR → 레이아웃 영역 → OCR 배정/미배정 → 진단 → 대조 화면.

예:
  .venv/Scripts/python.exe scripts/run_stage1.py --manifest fixtures/manifest.json --manifest fixtures/manifest-hwp.json \
      --out work/runs/stage1-base
  .venv/Scripts/python.exe scripts/run_stage1.py --rebuild work/runs/stage1-base   # 저장 응답으로 재빌드(호출 없음)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import stage1  # noqa: E402
from layout_pipeline.assignment import INK_RULE, OVERLAP_RULE, AssignmentRule  # noqa: E402
from layout_pipeline.config import get_settings  # noqa: E402
from layout_pipeline.viewer import build_viewer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", action="append", type=Path, default=[])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rebuild", type=Path, help="기존 실행 폴더를 저장된 원시 응답으로 재빌드")
    ap.add_argument("--pdf-dpi", type=int, default=200)
    ap.add_argument("--routing", type=Path, default=ROOT / "config" / "routing.json")
    ap.add_argument("--boundary-plans", type=Path, default=None, help="boundary-plan/1 JSON 폴더(긴 이미지 확정 경계)")
    ap.add_argument("--layout-threshold", type=float, default=None, help="비교 실험용. 기본은 요청에 넣지 않음")
    ap.add_argument("--layout-option", action="append", default=[],
                    help='비교 실험용 레이아웃 요청 옵션 KEY=JSON (예: layoutMergeBboxesMode="large")')
    ap.add_argument("--layout-config", default=str(ROOT / "config" / "layout-options.json"),
                    help="기본 레이아웃 요청 옵션 파일. none 이면 서버 기본(요청에 넣지 않음)")
    ap.add_argument("--pages", default="", help="쉼표로 구분한 page_id 만 처리")
    ap.add_argument("--core-shrink", type=float, default=AssignmentRule.core_shrink)
    ap.add_argument("--assignment-rule", choices=["core", "ink-containment", "overlap-best"], default="core",
                    help="core: 엄격 포함(기본), ink-containment: 글자 잉크 포함, overlap-best: 겹친 OCR 줄을 최적 단일 영역에 귀속")
    ap.add_argument("--dest", type=Path, default=None, help="--rebuild 결과를 쓸 새 폴더(원래 실행 폴더 보존)")
    ap.add_argument("--ocr-concurrency", type=int, default=1, help="동시성 비교 실험용(기본 1: 서버 추론 직렬)")
    args = ap.parse_args()
    rule = (INK_RULE if args.assignment_rule == "ink-containment" else
            OVERLAP_RULE if args.assignment_rule == "overlap-best" else AssignmentRule(core_shrink=args.core_shrink))
    layout_options = {}
    if args.layout_config != "none":
        layout_options |= json.loads(Path(args.layout_config).read_text(encoding="utf-8"))["layout_options"]
    if args.layout_threshold is not None:
        layout_options["layoutThreshold"] = args.layout_threshold
    for item in args.layout_option:
        key, value = item.split("=", 1)
        layout_options[key] = json.loads(value)
    if args.rebuild:
        out = args.dest or args.rebuild
        if args.dest and args.dest.exists() and any(args.dest.iterdir()):
            ap.error(f"--dest 폴더가 비어 있지 않습니다: {args.dest}")
        report = stage1.rebuild(args.rebuild, rule, args.dest, args.boundary_plans)
    else:
        if not args.manifest or not args.out:
            ap.error("--manifest 와 --out 이 필요합니다")
        if args.out.exists() and any(args.out.iterdir()):
            ap.error(f"출력 폴더가 비어 있지 않습니다(측정 덮어쓰기 방지): {args.out}")
        out = args.out
        report = stage1.run(args.manifest, out, get_settings(), pdf_dpi=args.pdf_dpi, policy_path=args.routing,
                            plans_dir=args.boundary_plans, layout_options=layout_options,
                            only_pages={p for p in args.pages.split(",") if p} or None, rule=rule,
                            ocr_concurrency=args.ocr_concurrency)
    viewer = build_viewer(out)
    print(report["markdown"])
    print(json.dumps({"viewer": str(viewer), "totals": report["totals"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
