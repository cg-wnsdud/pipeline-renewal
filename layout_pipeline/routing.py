"""입력 경로 결정과 모델 입력 범위(InputView).

비율 기반 분기만 여기서 결정한다. 모델별 해상도 조건은 분기에 쓰지 않는다.
- whole: 원래 공간 배치를 유지한 페이지 전체를 OCR 1회에 보낸다(일반 페이지 기본).
- long : 경계 탐색·검증이 끝난 boundary-plan(status=validated)이 있을 때만 그 타일로 OCR 한다.
         계획이 없거나 검증되지 않았으면 상태를 명시하고 입력 범위를 만들지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .jsonio import load
from .pages import Page

POLICY_CONTRACT = "routing-policy/1"
PLAN_CONTRACT = "boundary-plan/2"
PLAN_STATUSES = {"validated", "candidate", "no_safe_boundary", "oversized_region"}


class RoutingConfigError(ValueError):
    pass


class BoundaryPlanError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingPolicy:
    long_aspect_min: float
    square_max_aspect: float
    a4_aspect_range: tuple[float, float]
    status: str
    path: str
    long_hold_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load_policy(path: Path) -> RoutingPolicy:
    data = load(path)
    if data.get("contract") != POLICY_CONTRACT:
        raise RoutingConfigError(f"routing 계약이 아닙니다: {path}")
    labels = data["shape_labels"]
    policy = RoutingPolicy(float(data["long_aspect_min"]), float(labels["square_max_aspect"]),
                           tuple(float(v) for v in labels["a4_aspect_range"]), data["status"], str(path),
                           str(data.get("long_route", {}).get("hold_reason", "")))
    # A4·정사각형은 어떤 설정에서도 whole 이어야 한다.
    if policy.long_aspect_min <= max(policy.a4_aspect_range[1], policy.square_max_aspect):
        raise RoutingConfigError("long_aspect_min 이 A4/정사각형 범위를 침범합니다")
    return policy


def shape_class(aspect: float, policy: RoutingPolicy) -> str:
    if aspect <= policy.square_max_aspect:
        return "square"
    lo, hi = policy.a4_aspect_range
    if lo <= aspect <= hi:
        return "a4"
    return "long" if aspect >= policy.long_aspect_min else "general"


@dataclass(frozen=True)
class InputView:
    view_id: str
    page_id: str
    kind: str                      # whole | tile
    crop: tuple[int, int, int, int]  # 페이지 좌표 x0, y0, x1, y1

    @property
    def offset(self) -> tuple[int, int]:
        return self.crop[0], self.crop[1]

    @property
    def size(self) -> tuple[int, int]:
        return self.crop[2] - self.crop[0], self.crop[3] - self.crop[1]

    def to_dict(self) -> dict:
        w, h = self.size
        return {"view_id": self.view_id, "page_id": self.page_id, "kind": self.kind, "crop": list(self.crop),
                "width": w, "height": h, "aspect": round(max(w, h) / min(w, h), 4)}


@dataclass
class RouteDecision:
    page_id: str
    route: str          # whole | long
    shape: str
    # 실행 상태: READY | BOUNDARY_PENDING | BOUNDARY_CANDIDATE_UNVALIDATED | NO_SAFE_BOUNDARY | OVERSIZED_REGION
    # | BOUNDARY_PLAN_INVALID. READY 는 실행 가능/완료일 뿐 내용 품질 통과가 아니다.
    status: str
    basis: str
    views: list[InputView]
    plan: dict | None = None

    def to_dict(self) -> dict:
        return {"page_id": self.page_id, "route": self.route, "shape": self.shape, "status": self.status,
                "basis": self.basis, "views": [v.to_dict() for v in self.views], "boundary_plan": self.plan,
                "vlm_pixel_budget_used": False}


REQUIRED_PLAN_FIELDS = ("plan_version", "page_id", "page_image_sha256", "page_size", "coordinate_system", "axis",
                        "policy", "status", "cuts", "boundaries")
COORDINATE_SYSTEM = "page_px_xyxy"
# validated 로 받을 수 있는 의미 경계 검증 방식. VLM 단독 판정·반복 합의는 실제 실험(경계 VLM 12회)에서
# 제목-설명 분리를 안전으로 오판했으므로 포함하지 않는다. VLM 출력은 candidate 로만 저장한다.
ACCEPTED_SEMANTIC_METHODS = {"human_review", "approved_auto_policy"}


def auto_policy_accepts(plan: dict, page: Page) -> bool:
    """approved_auto_policy 경계는 config/boundary-policy.json 의 정책이 사용자 승인·검증(passed)을 마쳤고,
    계획의 정책 이름·버전이 같고, 페이지가 검증 범위 안일 때만 받는다(docs/09 §2)."""
    from .auto_boundary import in_scope, load_policy, policy_usable
    pol = load_policy()
    return (policy_usable(pol) and plan["policy"].get("name") == pol["name"]
            and str(plan["policy"].get("version")) == str(pol["version"]) and in_scope(pol, page.width, page.height))


def tile_lengths(cuts: list[int]) -> list[int]:
    return [b - a for a, b in zip(cuts, cuts[1:])]


def check_sequence(cuts: list, extent: int, cross: int, policy: dict) -> list[str]:
    """전체 경계열 검사: 0~extent 연속·중복 없음·정수 증가, 모든 타일이 선언한 정책 한도 안."""
    problems = []
    if len(cuts) < 2 or cuts[0] != 0 or cuts[-1] != extent:
        problems.append(f"cuts 는 0 에서 {extent} 까지 페이지 전체를 덮어야 합니다: {cuts}")
    if any(not isinstance(c, int) or isinstance(c, bool) for c in cuts) or any(b <= a for a, b in zip(cuts, cuts[1:])):
        problems.append(f"cuts 는 엄격히 증가하는 정수여야 합니다(최종 타일 중복 금지): {cuts}")
        return problems
    max_len, max_aspect = policy.get("max_tile_extent_px"), policy.get("max_tile_aspect")
    if max_len is None and max_aspect is None:
        problems.append("정책 한도(max_tile_extent_px 또는 max_tile_aspect)가 없습니다")
    for i, length in enumerate(tile_lengths(cuts)):
        if max_len is not None and length > max_len:
            problems.append(f"타일 {i} 길이 {length} > 정책 한도 {max_len}")
        if max_aspect is not None and max(length, cross) / min(length, cross) > max_aspect:
            problems.append(f"타일 {i} 비율 {max(length, cross) / min(length, cross):.3f} > 정책 한도 {max_aspect}")
    return problems


def validate_plan(plan: dict, page: Page) -> list[int]:
    """경계 문서를 검사하고 cut 좌표를 돌려준다. 경계를 고르거나 고치지 않는다.

    validated 는 원본 해시·크기·좌표계·계획 버전·정책 한도·경계별 근거와 승인된 의미 검증 방식이 모두 있어야 한다.
    문자열 status 만으로 실행하지 않는다."""
    if plan.get("contract") != PLAN_CONTRACT:
        raise BoundaryPlanError(f"{PLAN_CONTRACT} 계약이 아닙니다")
    missing = [k for k in REQUIRED_PLAN_FIELDS if plan.get(k) in (None, "", [])]
    if plan.get("status") in ("candidate", "no_safe_boundary", "oversized_region"):
        missing = [k for k in missing if k not in ("cuts", "boundaries")]
    if missing:
        raise BoundaryPlanError(f"필수 필드 누락: {missing}")
    if plan["page_id"] != page.page_id:
        raise BoundaryPlanError(f"page_id 불일치: {plan['page_id']} != {page.page_id}")
    if list(plan["page_size"]) != [page.width, page.height]:
        raise BoundaryPlanError(f"page_size 불일치: {plan['page_size']}")
    if plan["page_image_sha256"] != page.image_sha256:
        raise BoundaryPlanError("page_image_sha256 불일치: 원본/렌더가 달라진 경계입니다")
    if plan["coordinate_system"] != COORDINATE_SYSTEM:
        raise BoundaryPlanError(f"coordinate_system 은 {COORDINATE_SYSTEM} 이어야 합니다")
    if plan["status"] not in PLAN_STATUSES:
        raise BoundaryPlanError(f"알 수 없는 status: {plan['status']}")
    policy = plan["policy"]
    if not policy.get("name") or not policy.get("version"):
        raise BoundaryPlanError("policy.name/version 이 필요합니다")
    axis = plan["axis"]
    long_axis = "y" if page.height >= page.width else "x"
    if axis != long_axis:
        raise BoundaryPlanError(f"axis={axis} 는 긴 축({long_axis})이 아닙니다")
    cuts = list(plan.get("cuts") or [])
    if plan["status"] != "validated":
        return cuts
    extent, cross = (page.height, page.width) if axis == "y" else (page.width, page.height)
    problems = check_sequence(cuts, extent, cross, policy)
    internal = cuts[1:-1]
    bounds = plan["boundaries"]
    if sorted(b.get("position") for b in bounds) != internal:
        problems.append(f"경계별 근거의 position 이 내부 cut 과 다릅니다: {[b.get('position') for b in bounds]} vs {internal}")
    auto_ok = auto_policy_accepts(plan, page)
    for b in bounds:
        sem = b.get("semantic_check") or {}
        if not b.get("evidence"):
            problems.append(f"경계 {b.get('position')}: 근거(evidence) 없음")
        if sem.get("method") == "approved_auto_policy" and not auto_ok:
            problems.append(f"경계 {b.get('position')}: 자동 정책이 승인·검증되지 않았거나 검증 범위 밖")
        elif sem.get("method") not in ACCEPTED_SEMANTIC_METHODS or not sem.get("verified_by"):
            problems.append(f"경계 {b.get('position')}: 승인된 의미 검증 방식이 아님({sem.get('method')}). "
                            "VLM 판정은 candidate 로만 저장한다")
        if sem.get("result") != "safe":
            problems.append(f"경계 {b.get('position')}: 의미 검증 결과가 safe 가 아님({sem.get('result')})")
    if problems:
        raise BoundaryPlanError("; ".join(problems))
    return cuts


def candidate_views(plan: dict, page: Page) -> list[InputView]:
    """후보 경계 실험 실행용 타일. 원본을 빈틈·중복 없이 덮는지만 검사한다(타일 길이는 후보 정책의 목표값이라 강제하지 않음).

    결과는 CANDIDATE_RUN 으로만 쓰이며 validated·human_review 로 바뀌지 않는다."""
    cuts = list(plan.get("cuts") or [])
    extent = page.height if plan["axis"] == "y" else page.width
    problems = check_sequence(cuts, extent, page.width if plan["axis"] == "y" else page.height,
                              {"max_tile_extent_px": extent})
    if problems:
        raise BoundaryPlanError("; ".join(problems))
    views = []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        crop = (0, a, page.width, b) if plan["axis"] == "y" else (a, 0, b, page.height)
        views.append(InputView(f"{page.page_id}-t{i}", page.page_id, "candidate_tile", crop))
    return views


def decide(page: Page, policy: RoutingPolicy, plans: dict[str, dict], run_candidates: bool = False) -> RouteDecision:
    aspect = page.aspect
    shape = shape_class(aspect, policy)
    basis = (f"aspect={aspect:.4f}({page.width}x{page.height}, {page.orientation}); "
             f"long_aspect_min={policy.long_aspect_min} [{policy.status}]")
    if shape != "long":
        view = InputView(f"{page.page_id}-v0", page.page_id, "whole", (0, 0, page.width, page.height))
        return RouteDecision(page.page_id, "whole", shape, "READY", basis + " → 전체 페이지 OCR 1회", [view])
    plan = plans.get(page.page_id)
    other_image = plan is not None and bool(plan.get("page_image_sha256")) and plan["page_image_sha256"] != page.image_sha256
    if plan is None or other_image:
        # 경계 문서는 page_id 로 찾지만 원본 이미지 해시가 다르면 다른 입력의 문서다(새 입력의 file_id 가 우연히 같은 경우).
        # 그 문서는 이 페이지에 적용하지 않고 경계 없음으로 둔다(자동 경계 정책 대상).
        return RouteDecision(page.page_id, "long", shape, "BOUNDARY_PENDING",
                             basis + (" → 같은 page_id 의 경계 문서는 다른 이미지(해시 불일치)라 적용하지 않음" if other_image else "")
                             + " → 확정 경계 없음: OCR 미실행"
                             + (f" ({policy.long_hold_reason})" if policy.long_hold_reason else ""), [])
    try:
        cuts = validate_plan(plan, page)
    except BoundaryPlanError as exc:
        return RouteDecision(page.page_id, "long", shape, "BOUNDARY_PLAN_INVALID",
                             basis + f" → 경계 문서 거부: {exc}", [], plan)
    status = plan["status"]
    if status == "candidate" and run_candidates and plan.get("cuts"):
        try:
            views = candidate_views(plan, page)
        except BoundaryPlanError as exc:
            return RouteDecision(page.page_id, "long", shape, "BOUNDARY_PLAN_INVALID",
                                 basis + f" → 후보 경계 문서 거부: {exc}", [], plan)
        return RouteDecision(page.page_id, "long", shape, "CANDIDATE_RUN",
                             basis + f" → 미검증 후보 경계로 실험 실행({len(views)}개 타일). 확정 결과 아님", views, plan)
    if status != "validated":
        state = {"candidate": "BOUNDARY_CANDIDATE_UNVALIDATED", "no_safe_boundary": "NO_SAFE_BOUNDARY",
                 "oversized_region": "OVERSIZED_REGION"}[status]
        return RouteDecision(page.page_id, "long", shape, state, basis + f" → 경계 상태 {status}: OCR 미실행"
                             + (f" ({policy.long_hold_reason})" if policy.long_hold_reason else ""), [], plan)
    views = []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        crop = (0, a, page.width, b) if plan["axis"] == "y" else (a, 0, b, page.height)
        views.append(InputView(f"{page.page_id}-t{i}", page.page_id, "tile", crop))
    return RouteDecision(page.page_id, "long", shape, "READY",
                         basis + f" → 확정 경계 {len(views)}개 타일 OCR", views, plan)


def load_plans(directory: Path | None) -> dict[str, dict]:
    if directory is None:
        return {}
    plans = {}
    for path in sorted(Path(directory).glob("*.json")):
        plan = load(path)
        if plan.get("contract") == PLAN_CONTRACT:
            plans[plan["page_id"]] = plan | {"_path": str(path)}
    return plans


def compose_sections(extent: int, safe_positions: list[int], target_len: int) -> dict:
    """섹션 묶음(section-pack/1, 실험 정책): 안전 후보 위치로 나뉜 연속 섹션을 target_len 안에서 묶는다.

    target_len 은 상한이 아니라 묶음 목표다. 안쪽에 안전 후보가 없는 섹션이 target_len 보다 길면 그 섹션 하나를
    통째로 한 타일로 둔다(문장·표를 임의 위치에서 자르지 않음). 동적 계획법: 타일 수 최소, 같으면 가장 긴 타일 최소."""
    nodes = [0] + sorted({p for p in safe_positions if 0 < p < extent}) + [extent]
    best: dict[int, tuple[int, int, int | None]] = {0: (0, 0, None)}
    for i, p in enumerate(nodes):
        if p not in best:
            continue
        tiles, longest, _ = best[p]
        for j in range(i + 1, len(nodes)):
            q = nodes[j]
            if q - p > target_len and j > i + 1:
                break
            cand = (tiles + 1, max(longest, q - p), p)
            cur = best.get(q)
            if cur is None or cand[:2] < cur[:2]:
                best[q] = cand
    cuts, cur = [], extent
    while cur is not None:
        cuts.append(cur)
        cur = best[cur][2]
    cuts.reverse()
    lengths = tile_lengths(cuts)
    return {"status": "composed", "cuts": cuts, "tile_lengths": lengths, "target_len": target_len,
            "oversized_sections": [[a, b] for a, b in zip(cuts, cuts[1:]) if b - a > target_len]}
