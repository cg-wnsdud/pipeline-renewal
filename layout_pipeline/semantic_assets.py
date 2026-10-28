"""의미 단계(분류·라벨링) 데이터 자산: 분류 정책·라벨 스키마·템플릿 카탈로그·퇴직연금 구성(assets/semantic).

- 라벨 스키마·카탈로그·퇴직연금 구성은 nh-ad-compliance 에서 복사한 데이터다(scripts/import_semantic_assets.py,
  출처·커밋·SHA-256 은 manifest.json). 분류 정책(classification-policy.json)은 이 저장소에서 관리한다.
- 실행·테스트는 같은 복사본을 읽는다. 로드할 때 manifest 해시와 비교해 다르면 멈춘다(다른 버전이 섞이지 않게).
- 라벨 지식 글(knowledge)은 스키마의 구분값별 템플릿 예시문구·실제 광고 예시·반례로만 만든다. 예시의 출처 광고 이름
  (source_ad)은 넣지 않고, 예시 문구를 중간에서 자르지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .jsonio import load, sha256_file

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "semantic"
# 이번 범위의 템플릿 스키마(원본 라벨 스키마 19개 중 10개). 운용상품 ETF·ELB 전용 스키마는 원본에도 없다.
LABEL_SCHEMA_KEYS = ("loan-named", "loan-broker-balance", "deposit-demand", "deposit-term", "deposit-saving",
                     "deposit-index-linked", "investment-isa-general", "investment-isa-trust",
                     "investment-isa-discretionary", "investment-pension-general")
SPECIAL_CODES = ("unsupported", "undetermined")


def verify_assets() -> list[str]:
    """복사본이 manifest 의 SHA-256 과 같은지. 다르면 오류 목록."""
    path = ASSET_DIR / "manifest.json"
    if not path.is_file():
        return ["assets/semantic/manifest.json 없음(scripts/import_semantic_assets.py 실행 필요)"]
    man = load(path)
    errs = []
    for rel, meta in list(man["files"].items()) + list(man.get("local_files", {}).items()):
        f = ASSET_DIR / rel
        if not f.is_file():
            errs.append(f"{rel}: 파일 없음")
        elif sha256_file(f) != meta["sha256"]:
            errs.append(f"{rel}: SHA-256 이 manifest 와 다름")
    missing = set(LABEL_SCHEMA_KEYS) - set(man.get("label_schemas", []))
    if missing:
        errs.append(f"manifest 에 없는 라벨 스키마 {sorted(missing)}")
    return errs


@lru_cache(maxsize=1)
def manifest() -> dict:
    errs = verify_assets()
    if errs:
        raise RuntimeError("의미 단계 자산이 manifest 와 다릅니다: " + "; ".join(errs))
    return load(ASSET_DIR / "manifest.json")


@lru_cache(maxsize=1)
def policy() -> dict:
    manifest()
    return load(ASSET_DIR / "classification-policy.json")


@lru_cache(maxsize=None)
def label_schema(key: str) -> dict:
    if key not in LABEL_SCHEMA_KEYS:
        raise KeyError(f"이번 범위에 없는 라벨 스키마 {key}")
    manifest()
    return load(ASSET_DIR / "label-schemas" / f"{key}.json")


@lru_cache(maxsize=1)
def retirement_context() -> dict:
    """product-contexts-v1 의 퇴직연금 공통 구성: 기본 템플릿과 운용상품(FUND·ETF·ELB) 템플릿."""
    manifest()
    ctx = load(ASSET_DIR / "product-contexts-v1.json")
    return next(c for c in ctx["contexts"] if c["code"] == "RETIREMENT")


def versions() -> dict:
    """결과·trace 에 남길 자산 버전. digest 는 manifest 파일 해시(복사본 전체 해시를 담고 있음)."""
    man = manifest()
    return {"assets": man["version"], "policy": policy()["version"],
            "assets_digest": sha256_file(ASSET_DIR / "manifest.json")[:16]}


def templates() -> list[dict]:
    return policy()["templates"]


def template(code: str) -> dict:
    return next(t for t in templates() if t["code"] == code)


def template_codes() -> list[str]:
    return [t["code"] for t in templates()]


def feature_ids() -> list[str]:
    """템플릿 특징 ID + 범위 밖·미확정 대상의 근거 ID(other.*)."""
    return [f["id"] for t in templates() for f in t["features"]] + [f["id"] for f in policy().get("other_features", [])]


def feature_template(feature_id: str) -> str:
    return feature_id.split(".", 1)[0]


def component_templates() -> dict[str, str]:
    """운용상품 코드 → 템플릿 ID(FUND → 투자성상품-펀드 …)."""
    return {c["code"]: c["template"] for c in retirement_context()["components"]}


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def merged_schema(keys: list[str]) -> dict:
    """한 상품에 템플릿이 여럿이면(예: 대출모집인 + 상품명 노출) 스키마를 합친다. 같은 예시는 한 번만."""
    schemas = [label_schema(k) for k in dict.fromkeys(keys)]
    labels, items, pos, neg, seen = [], [], [], [], set()
    for s in schemas:
        labels += [x for x in s["labels"] if x not in labels]
        for kind, rows, out in (("t", s["template_items"], items), ("p", s["positives"], pos), ("n", s["negatives"], neg)):
            for row in rows:
                clean = {k: v for k, v in row.items() if k != "source_ad"}   # 출처 광고 이름은 모델 입력에 넣지 않는다
                fp = kind + _fingerprint(clean)
                if fp not in seen:
                    seen.add(fp)
                    out.append(clean)
    return {"keys": [s["key"] for s in schemas], "scope": " + ".join(s["scope"] for s in schemas), "labels": labels,
            "template_items": items, "positives": pos, "negatives": neg}


def knowledge(keys: list[str]) -> tuple[list[str], str]:
    """(허용 라벨, 라벨 지식 글). 구분값마다 템플릿 예시문구와 실제 광고 예시를 묶고 반례는 끝에 모은다."""
    s = merged_schema(keys)
    lines = [f"적용 템플릿: {s['scope']}", "[구분값별 예시]"]
    for label in s["labels"]:
        lines.append(f"- {label}")
        tpl = [i["example"] for i in s["template_items"] if i["gubun"] == label]
        if tpl:
            lines.append("  템플릿 예시문구:")
            lines += [f"  · {x}" for x in tpl]
        ex = []
        for row in s["positives"]:
            if row["labels"] and row["labels"][0] == label:
                multi = f" → labels={json.dumps(row['labels'], ensure_ascii=False)}" if len(row["labels"]) > 1 else ""
                ex.append(f"  · \"{' '.join(row['quote'].split())}\"{multi}")
            elif label in row["labels"][1:]:
                ex.append(f"  · ({row['labels'][0]} 예시의 같은 문구에 함께 붙은 라벨)")
        if ex:
            lines += ["  실제 광고 예시(다른 광고의 문구):", *ex]
    if s["negatives"]:
        lines += ["[반례: 헷갈리기 쉽지만 확인된 문구]"]
        for row in s["negatives"]:
            note = row.get("note")
            if note is None:
                c = row.get("contrast")
                note = f"{c} 아님" if c and c not in row["labels"] else ("구분값 아님" if not row["labels"] else "")
            tail = f" ({note})" if note else ""
            lines.append(f"- \"{' '.join(row['quote'].split())}\" → labels={json.dumps(row['labels'], ensure_ascii=False)}{tail}")
    return s["labels"], "\n".join(lines)
