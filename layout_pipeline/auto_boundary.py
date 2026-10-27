"""자동 경계 정책 section-pack/2(docs/09 §2). 승인 계획이 없는 긴 이미지의 타일 경계를 만든다.

1) 탐색 OCR(폭×2.5 창, 50% 겹침) → 2) 관측 상자를 가로지르지 않는 빈 띠 → 3) compose_sections(목표 폭×2.5)
→ 4) 고른 경계만 VLM 관계 판정(relation/2) → 허용 관계가 아니면 그 위치를 빼고 다시 구성.
탐색 결과는 경계를 고르는 데만 쓰며 최종 영역이 아니다. 승인·검증 범위(config/boundary-policy.json)를
만족할 때만 validated(approved_auto_policy), 그 밖에는 candidate 다.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw

from . import boundary
from .jsonio import dump, load
from .routing import PLAN_CONTRACT, InputView, compose_sections

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "boundary-policy.json"
RELATION_VERSION = "relation/2"
MAX_ROUNDS = 10


def load_policy() -> dict:
    return load(POLICY_PATH)


def policy_usable(policy: dict) -> bool:
    """사용자 승인과 정책 검증(docs/09 §2-6)이 모두 끝난 정책만 validated 계획을 만들 수 있다."""
    return policy.get("status") == "approved" and (policy.get("validation") or {}).get("status") == "passed"


def in_scope(policy: dict, width: int, height: int) -> bool:
    sc = policy.get("scope") or {}
    lo, hi = sc.get("width_px", [0, 10 ** 9])
    return lo <= width <= hi and height / width >= sc.get("min_aspect", 0)


def relation_request(page_img: Image.Image, y: int, above: list[dict], below: list[dict], relations: list[str]) -> dict:
    top, bot = max(0, y - boundary.CONTEXT_PX), min(page_img.height, y + boundary.CONTEXT_PX)
    crop = page_img.crop((0, top, page_img.width, bot))
    marked = crop.copy()
    ImageDraw.Draw(marked).line((0, y - top, crop.width, y - top), fill=(220, 0, 0), width=3)
    schema = {"type": "object", "additionalProperties": False, "required": ["above_id", "below_id", "relation", "reason"],
              "properties": {"above_id": {"type": "string", "enum": [a["id"] for a in above] + ["NONE"]},
                             "below_id": {"type": "string", "enum": [b["id"] for b in below] + ["NONE"]},
                             "relation": {"type": "string", "enum": relations}, "reason": {"type": "string"}}}
    text = ("긴 광고 화면을 가로로 나눌 위치를 검토합니다. 첫 이미지는 후보선 주변 원본, 둘째는 같은 이미지에 후보선(빨간 선)만 "
            "그린 사본입니다. 글자는 첫 이미지에서 읽으세요.\n"
            "1) above_id: 선 바로 위에서 판단 근거가 되는 글자(A 목록, A1 이 선에 가장 가까움). 없으면 NONE.\n"
            "2) below_id: 선 바로 아래의 근거 글자(B 목록). 없으면 NONE.\n"
            "3) relation: 위 내용과 아래 내용의 관계를 하나 고르세요.\n"
            "- independent_sections: 서로 독립된 섹션(위 섹션이 끝나고 새 섹션이 시작)\n"
            "- table_and_footnote: 위가 표(또는 표의 마지막 행)이고 아래가 그 표의 각주·주석\n"
            "- menu_or_tab_and_content: 위가 탭·메뉴이고 아래가 그 내용\n"
            "- button_and_description: 버튼·링크와 그 안내문 사이\n"
            "- list_items: 같은 목록·각주 목록·버튼 목록의 서로 다른 항목 사이\n"
            "- heading_and_body: 위가 제목·소제목이고 아래가 그 설명·내용\n"
            "- same_paragraph_or_sentence: 같은 문단·문장이 이어짐(한 항목의 설명이 이어지는 경우 포함)\n"
            "- inside_table: 같은 표·항목-값 목록의 행 사이\n"
            "- unknown: 판단할 수 없음\n"
            "좌표나 새 후보를 만들지 마세요. reason 에 근거를 짧게 쓰세요.\n"
            "위 글자(A, 선에서 가까운 순): " + json.dumps(above, ensure_ascii=False) +
            "\n아래 글자(B, 선에서 가까운 순): " + json.dumps(below, ensure_ascii=False))
    return {"text": text, "images": [crop, marked], "schema": schema}


def judge(parsed, above, below, allowed: set[str]) -> tuple[bool, str]:
    """코드가 관계 유형으로 판정한다. 근거 ID 가 선에 가장 가까운 3개 밖이면 버린다(evidence_mismatch)."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("relation"), str):
        return False, "invalid_response"
    near_a = {a["id"] for a in above[:3]} | ({"NONE"} if not above else set())
    near_b = {b["id"] for b in below[:3]} | ({"NONE"} if not below else set())
    if parsed.get("above_id") not in near_a or parsed.get("below_id") not in near_b:
        return False, "evidence_mismatch"
    return parsed["relation"] in allowed, parsed["relation"]


def scout(page, ocr_client, out_dir: Path) -> tuple[list[dict], list[dict]]:
    """탐색 OCR. 반환: 관측 상자(잘린 관측 제외), 호출 기록."""
    boxes, calls = [], []
    for i, crop in enumerate(boundary.scout_windows(page.width, page.height)):
        view = InputView(f"{page.page_id}-s{i}", page.page_id, "scout", crop)
        rec, pruned = ocr_client.call(page, view)
        calls.append(asdict(rec))
        if pruned is not None:
            boxes += boundary.observed_boxes(pruned, view, page.height)
    return boxes, calls


def make_plan(page, boxes: list[dict], vlm_client, out_dir: Path, policy: dict, scout_calls: list[dict]) -> dict:
    """빈 띠 후보 → 구성 → 고른 경계만 관계 판정 → 거부 위치를 빼고 다시 구성."""
    allowed = set(policy["allowed_relations"])
    relations = policy["allowed_relations"] + policy["rejected_relations"]
    cands, line_gap = boundary.candidates(boxes, page.height)
    target = round(page.width * policy["target_aspect"])
    pool = {c["y"]: c for c in cands}
    judged: dict[int, dict] = {}
    img = page.open_image()
    rounds = []
    comp = None
    for _ in range(MAX_ROUNDS):
        comp = compose_sections(page.height, [y for y in pool if judged.get(y, {}).get("ok", True)], target)
        todo = [y for y in comp["cuts"][1:-1] if y not in judged]
        rounds.append({"cuts": comp["cuts"], "to_judge": todo})
        if not todo:
            break
        for y in todo:
            above, below = boundary.context_items(boxes, y)
            req = relation_request(img, y, above, below, relations)
            call = vlm_client.chat(req["text"], req["images"], req["schema"], "boundary_relation", 800,
                                   {"kind": "boundary_relation", "version": RELATION_VERSION, "page_id": page.page_id, "y": y})
            dump(out_dir / "boundary-calls" / f"{call.call_id}.json", asdict(call))
            ok, relation = judge(call.parsed, above, below, allowed)
            judged[y] = {"ok": ok, "relation": relation, "call_id": call.call_id,
                         "reason": (call.parsed or {}).get("reason") if isinstance(call.parsed, dict) else None}
    # 목표보다 긴 타일이 남으면(안쪽에 허용 경계 없음) 레이아웃 검출 품질을 보장할 수 없어 자동 확정하지 않는다(f09 A 실측).
    final_ok = comp is not None and not comp["oversized_sections"] and all(judged.get(y, {}).get("ok") for y in comp["cuts"][1:-1])
    scope_ok = in_scope(policy, page.width, page.height)
    status = "validated" if final_ok and scope_ok and policy_usable(policy) else "candidate"
    bnds = []
    for y in comp["cuts"][1:-1]:
        c, j = pool[y], judged.get(y, {})
        top, bot = max(0, y - 220), min(page.height, y + 220)
        ctx = img.crop((0, top, page.width, bot)).copy()
        ImageDraw.Draw(ctx).line((0, y - top, page.width, y - top), fill=(220, 0, 0), width=3)
        ctx_path = out_dir / "boundary-evidence" / f"{page.page_id}-y{y}.png"
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.save(ctx_path)
        bnds.append({"position": y,
                     "evidence": {"geometry": {"gap_px": c["gap_px"], "crossing_scout_boxes": 0, "line_gap_median": line_gap},
                                  "context_image": ctx_path.as_posix()},
                     "semantic_check": {"method": "approved_auto_policy", "policy": f"{policy['name']}/{policy['version']}",
                                        "verified_by": policy.get("approved_by"), "relation": j.get("relation"),
                                        "result": "safe" if j.get("ok") else "rejected", "call_id": j.get("call_id"),
                                        "reason": j.get("reason")}})
    return {"contract": PLAN_CONTRACT, "plan_version": f"auto-{policy['name']}-{policy['version']}",
            "page_id": page.page_id, "page_image_sha256": page.image_sha256, "page_size": [page.width, page.height],
            "coordinate_system": "page_px_xyxy", "axis": "y", "status": status,
            "policy": {"name": policy["name"], "version": policy["version"], "max_tile_aspect": policy["target_aspect"],
                       "target_tile_extent_px": target, "status": policy.get("status"),
                       "validation": (policy.get("validation") or {}).get("status")},
            "cuts": comp["cuts"], "boundaries": bnds, "composition": comp, "rounds": rounds,
            "rejected_positions": {str(y): j for y, j in judged.items() if not j["ok"]},
            "scope_ok": scope_ok, "scout_calls": [c["call_id"] for c in scout_calls],
            "note": "자동 경계 정책 결과. validated 는 승인된 정책·검증 범위 안에서만 붙는다." if status == "validated"
                    else "자동 경계 후보(정책 미승인·범위 밖·거부 경계 남음 중 하나). CANDIDATE 로만 실행한다."}
