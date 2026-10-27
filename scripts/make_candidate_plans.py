"""긴 이미지 후보 경계 계획(boundary-plan/2, status=candidate) 생성. docs/08 B.

- 기하: 기존 탐색(work/boundary/exp1/scout.json)의 빈 띠 후보와 관측 상자를 재사용한다.
- 의미: 기존 VLM 관계 판정(classify.json M2)을 재사용하고, 판정이 없는 위치만 새로 1회 호출한다.
  AI 시각 검토(fixtures/boundary-ai-review.json, 사람 검토 아님)와 M2 가 모두 독립 섹션이고 교차 상자가 없는 위치만 쓴다.
- 구성: routing.compose_sections(section-pack/1, 목표 길이 = 폭×2.5, 안쪽 후보가 없는 긴 섹션은 통째로).
- 결과는 candidate 다. validated·human_review 를 쓰지 않는다. 실행은 run_pipeline --run-candidate-boundaries 로만 한다.

사용: make_candidate_plans.py --out fixtures/boundary-plans-candidate --evidence work/long/candidates
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path

from PIL import ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline import boundary  # noqa: E402
from layout_pipeline.config import load_env_file  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.pages import Page  # noqa: E402
from layout_pipeline.routing import PLAN_CONTRACT, compose_sections  # noqa: E402
from layout_pipeline.vlm_client import VlmClient  # noqa: E402

SCOUT = ROOT / "work" / "boundary" / "exp1"
PAGES_JSON = ROOT / "work" / "runs" / "stage1-merge-large" / "pages.json"
TARGET_ASPECT = 2.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    a = ap.parse_args()
    load_env_file()
    scout = load(SCOUT / "scout.json")["pages"]
    classify = {round(c["y"]): c for c in load(SCOUT / "classify.json")["cases"]}
    review = load(ROOT / "fixtures" / "boundary-ai-review.json")
    pages = {p["page_id"]: Page(**{k: v for k, v in p.items() if k != "geometry"}) for p in load(PAGES_JSON)}
    client = VlmClient(os.environ["PN_VLM_URL"], os.environ["PN_VLM_MODEL"], float(os.environ.get("PN_VLM_TIMEOUT_S", "320")),
                       prefix="bnd2")
    a.evidence.mkdir(parents=True, exist_ok=True)
    for pid in sorted({p["page_id"] for p in review["positions"]}):
        page, sd = pages[pid], scout[pid]
        img = page.open_image()
        rows = []
        for rv in [p for p in review["positions"] if p["page_id"] == pid]:
            cand = min(sd["candidates"], key=lambda c: abs(c["y"] - rv["y"]))
            if abs(cand["y"] - rv["y"]) > 3:
                raise SystemExit(f"{pid} y={rv['y']}: 기하 후보 없음")
            y = cand["y"]
            crossing = [b for b in sd["boxes"] if b["bbox"][1] < y < b["bbox"][3]]
            prior = classify.get(y) if (classify.get(y) or {}).get("page_id") == pid else None
            if prior is not None:
                m2 = {"relation": prior["m2_relation"], "safe": prior["m2_safe"], "call_id": prior["call_id"],
                      "source": "work/boundary/exp1/classify.json(재사용)"}
            else:
                above, below = boundary.context_items(sd["boxes"], y)
                req = boundary.relation_request(img, y, above, below)
                call = client.chat(req["text"], req["images"], req["schema"], "boundary_relation", 800,
                                   {"page_id": pid, "y": y, "purpose": "candidate_plan"})
                dump(a.evidence / "calls" / f"{call.call_id}.json", asdict(call))
                safe, relation = boundary.accept_m2(call.parsed, above, below)
                m2 = {"relation": relation, "safe": safe, "call_id": call.call_id, "reason": (call.parsed or {}).get("reason"),
                      "source": "새 호출(2026-10-26)"}
            top, bot = max(0, y - 220), min(page.height, y + 220)
            ctx = img.crop((0, top, page.width, bot)).copy()
            ImageDraw.Draw(ctx).line((0, y - top, page.width, y - top), fill=(220, 0, 0), width=3)
            ctx_path = a.evidence / f"{pid}-y{y}.png"
            ctx.save(ctx_path)
            ok = rv["verdict"] == "section_boundary" and m2["safe"] and not crossing
            rows.append({"position": y, "usable": ok,
                         "evidence": {"geometry": {"scout_candidate_id": cand["id"], "gap_px": cand["gap_px"],
                                                   "crossing_scout_boxes": len(crossing)},
                                      "context_image": ctx_path.as_posix()},
                         "semantic_check": {"method": "vlm_relation+ai_visual_review", "result": "candidate_safe" if ok else "rejected",
                                            "verified_by": "VLM 관계 판정(M2) + Claude(AI) 시각 검토 — 사람 검토 아님",
                                            "m2": m2, "ai_review": rv["note"]}})
            print(pid, y, "M2", m2["relation"], "crossing", len(crossing), "usable", ok, flush=True)
        target = round(page.width * TARGET_ASPECT)
        comp = compose_sections(page.height, [r["position"] for r in rows if r["usable"]], target)
        used = set(comp["cuts"][1:-1])
        plan = {"contract": PLAN_CONTRACT, "plan_version": "section-pack-2026-10-26", "page_id": pid,
                "page_image_sha256": page.image_sha256, "page_size": [page.width, page.height],
                "coordinate_system": "page_px_xyxy", "axis": "y", "status": "candidate",
                "policy": {"name": "section-pack", "version": "1", "target_tile_extent_px": target,
                           "target_basis": "폭×2.5(탐색 창 실험의 잠정값). 상한이 아니라 묶음 목표",
                           "oversized_section_rule": "안쪽에 안전 후보가 없는 섹션은 목표보다 길어도 통째로 한 타일",
                           "status": "experimental"},
                "cuts": comp["cuts"], "composition": comp,
                "boundaries": [r for r in rows if r["position"] in used],
                "unused_positions": [r for r in rows if r["position"] not in used],
                "note": "미검증 후보. run_pipeline --run-candidate-boundaries 로만 실험 실행(CANDIDATE_RUN). validated 아님."}
        dump(a.out / f"{pid}.candidate.json", plan)
        print(pid, "cuts", comp["cuts"], "lengths", comp["tile_lengths"], "oversized", comp["oversized_sections"])
    print("VLM calls", client.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
