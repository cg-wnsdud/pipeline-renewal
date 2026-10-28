"""의미 단계(분류·라벨링) 데이터 자산 가져오기: nh-ad-compliance 의 템플릿 카탈로그·라벨 스키마·퇴직연금 구성 → assets/semantic/.

- 원본 저장소는 읽기만 한다(코드는 가져오지 않는다). 파일을 그대로 복사하고 출처(저장소 커밋·파일별 마지막 커밋·SHA-256)를
  assets/semantic/manifest.json 에 남긴다. 실행·테스트는 이 복사본과 manifest 만 읽는다(같은 버전).
- 라벨 스키마는 이번 범위의 템플릿 10개만 가져온다(대출 상품명 노출·대출모집인·예금성 4종·ISA 3종·퇴직연금 일반).
- --check: 복사본이 manifest 해시와 같은지만 본다(원본 저장소 없이도 가능).
- --refresh-local: 이 저장소에서 관리하는 분류 정책(classification-policy.json)을 고친 뒤 그 해시만 manifest 에 다시 쓴다.

예:
  .venv/Scripts/python.exe scripts/import_semantic_assets.py --source ../nh-ad-compliance
  .venv/Scripts/python.exe scripts/import_semantic_assets.py --check
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.jsonio import dump, sha256_file  # noqa: E402
from layout_pipeline.semantic_assets import ASSET_DIR, LABEL_SCHEMA_KEYS, verify_assets  # noqa: E402

FILES = {   # 복사본 경로(assets/semantic 기준) → 원본 경로(원본 저장소 기준)
    "ad_templates.json": "parser-pipeline/nh_parser_fin/templates/ad_templates.json",
    "product-contexts-v1.json": "packages/rule-engine/src/nh_rule_engine/data/product-contexts-v1.json",
    **{f"label-schemas/{k}.json": f"parser-pipeline/nh_parser_fin/label_schema/schemas/{k}.json" for k in LABEL_SCHEMA_KEYS},
}


def _git(src: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True, check=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, help="nh-ad-compliance 저장소(읽기 전용)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--refresh-local", action="store_true")
    args = ap.parse_args()
    if args.refresh_local:
        man = json.loads((ASSET_DIR / "manifest.json").read_text(encoding="utf-8"))
        for rel, meta in man["local_files"].items():
            meta["sha256"] = sha256_file(ASSET_DIR / rel)
        dump(ASSET_DIR / "manifest.json", man)
        print("local_files 해시 갱신: " + ", ".join(man["local_files"]))
        return 0
    if args.check:
        errs = verify_assets()
        print("\n".join(errs) if errs else "assets/semantic: manifest 해시와 같음")
        return 1 if errs else 0
    src = args.source
    if not src or not (src / ".git").exists():
        ap.error("--source 에 nh-ad-compliance 저장소 경로가 필요합니다")
    dirty = _git(src, "status", "--porcelain", "--", *FILES.values())
    if dirty:
        ap.error(f"원본 파일에 커밋되지 않은 변경이 있어 출처를 커밋으로 남길 수 없습니다:\n{dirty}")
    files = {}
    for rel, orig in FILES.items():
        dest = ASSET_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / orig, dest)
        files[rel] = {"source_path": orig, "source_commit": _git(src, "log", "-1", "--format=%H", "--", orig),
                      "source_commit_date": _git(src, "log", "-1", "--format=%cs", "--", orig), "sha256": sha256_file(dest)}
    head = _git(src, "rev-parse", "HEAD")
    schema_commit = max((files[f"label-schemas/{k}.json"] for k in LABEL_SCHEMA_KEYS), key=lambda f: f["source_commit_date"])
    manifest = {
        "asset_set": "nh-semantic-assets",
        "version": f"nh-label-schema@{schema_commit['source_commit'][:7]}+policy",
        "imported_at": date.today().isoformat(),
        "source": {"repository": "nh-ad-compliance", "head_commit": head,
                   "note": "읽기 전용 참고 저장소. 데이터 파일만 복사했고 코드·파싱 파이프라인은 가져오지 않았다."},
        "label_schemas": list(LABEL_SCHEMA_KEYS),
        "files": files,
        "local_files": {"classification-policy.json": {
            "note": "2026-10-27 작업 지시 7절의 사용자 제공 식별 기준을 옮긴 분류 정책(이 저장소에서 관리)",
            "sha256": sha256_file(ASSET_DIR / "classification-policy.json")}},
    }
    dump(ASSET_DIR / "manifest.json", manifest)
    print(json.dumps({"version": manifest["version"], "head": head[:7], "files": len(files)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
