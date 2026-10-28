"""입력별 run_pipeline 실행 여러 개를 입력 검토 화면 하나(report.html)로 모은다(모델 호출 없음).

사용: make_pipeline_report.py --out <합본 폴더> <실행 폴더> [<실행 폴더> ...]
  실행 폴더 = run_pipeline.py --out 결과(trace/timing.json 과 semantic/ 이 있는 곳). 입력(묶음)마다 따로 순서대로 실행하면
  입력별 전체 소요(OCR·판독·결과 저장·의미 단계)가 겹치지 않게 잡힌다.
출력(<합본 폴더>/):
  files/index.json        각 실행 semantic/files 의 result.json 을 상대 경로로 가리킨다(결과 파일을 복사·수정하지 않음)
  pipeline_timing.json    묶음별 파이프라인 시간(각 실행 trace/timing.json)
  report.json             묶음별 의미 단계 요약·시간·검사(각 실행 semantic/report.json 을 모음)
  report.html             입력 검토 화면(의미 단계 report.html 과 같은 양식 + 입력별 전체 소요)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.report_html import build_report, pipeline_entry  # noqa: E402

OWN = {"files", "pipeline_timing.json", "report.json", "report.html"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("runs", nargs="+", type=Path)
    args = ap.parse_args()
    out = args.out
    if out.exists() and {p.name for p in out.iterdir()} - OWN:
        ap.error(f"합본 폴더에 이 스크립트가 만들지 않은 파일이 있습니다(덮어쓰기 방지): {out}")
    files, reps, timing, contract = [], [], {}, None
    for run in args.runs:
        sem = run / "semantic"
        idx, rep = load(sem / "files" / "index.json"), load(sem / "report.json")
        contract = contract or idx["contract"]
        subs = list(dict.fromkeys(f["submission_id"] for f in idx["files"]))
        note = f"{run.name}: 입력 하나 실행" if len(subs) == 1 else f"{run.name}: 묶음 {len(subs)}개를 한 실행에서 함께 처리(묶음별로 나눌 수 없음)"
        entry = pipeline_entry(load(run / "trace" / "timing.json"), note)
        for s in subs:
            if s in timing:
                ap.error(f"submission_id {s} 가 여러 실행에 있습니다")
            timing[s] = entry
        files += [f | {"result": os.path.relpath(sem / f["result"], out).replace(os.sep, "/")} for f in idx["files"]]
        reps.append(rep)
    w = [r["timing"] for r in reps]
    report = {"contract": reps[0].get("contract"), "semantic_profile": reps[0].get("semantic_profile"), "versions": reps[0].get("versions"),
              "parse_dir": "입력별 run_pipeline 실행: " + ", ".join(r.name for r in args.runs),
              "timing": {"wall_seconds": round(sum(t["wall_seconds"] for t in w), 3), "http_calls": sum(t["http_calls"] for t in w),
                         "concurrency_limit": max(t["concurrency_limit"] for t in w), "peak_concurrency": max(t["peak_concurrency"] for t in w),
                         "replay": any(t.get("replay") for t in w), "note": "실행별 의미 단계 시간의 합(실행은 순서대로)"},
              "checks": {"passed": all(r["checks"]["passed"] for r in reps), "documents": sum(r["checks"]["documents"] for r in reps),
                         "problems": sum(r["checks"]["problems"] for r in reps)},
              "submissions": [s for r in reps for s in r["submissions"]]}
    if len({str(r.get("versions")) for r in reps}) > 1:
        report["versions_differ"] = [r.get("versions") for r in reps]
    dump(out / "files" / "index.json", {"contract": contract, "files": files})
    dump(out / "pipeline_timing.json", {"runs": [str(r) for r in args.runs], "submissions": timing})
    dump(out / "report.json", report)
    print(build_report(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
