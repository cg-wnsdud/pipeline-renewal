"""observe 모드(docs/16 §9): 관측 한 호출에서 상품·템플릿·모든 영역 소속 결정 → 상품 ID·영역 연결 검증 + 이름 없는 대상 정리(R1·R2)
→ 연결된 상품의 템플릿 스키마로 라벨링. 모델 호출 없음(가짜 클라이언트).

합성 입력은 단계 순서·분기 확인용이다. 실제 모델의 분류 품질 평가가 아니다.
"""
from layout_pipeline import classify, semantic
from layout_pipeline.jsonio import load
from test_semantic import EMPTY_LABELS, Fake, R, _enum, make_parse

TWO = [{"sub": "s2", "fid": "b01", "name": "x.png", "kind": "image",
        "pages": [[R("NH튼튼적금"), R("NH든든예금"), R("가입기간 12개월"), R("NH농협은행")]]}]


def T(tid, name, code, region, quote, feature=None):
    return {"tid": tid, "name": name, "group": "예금성", "templates": [code],
            "evidence": [{"feature": feature or f"{code}.name", "region": region, "quote": quote}]}


def observe_with(targets, regions):
    def fn(meta, schema, text):
        return {"targets": targets, "mentions": [], "parts": [],
                "regions": {a: regions.get(a, {"scope": "shared", "targets": []}) for a in _enum(schema)}}
    return fn


def run(tmp, observe, mode=None):
    fake = Fake(observe=observe, label=EMPTY_LABELS)
    rep = semantic.run(make_parse(tmp, TWO), tmp / "sem", fake, 2, **({} if mode is None else {"classification_mode": mode}))
    return rep, load(tmp / "sem" / "files" / "b01" / "result.json"), load(tmp / "sem" / "trace" / "semantic" / "s2.json")


def test_default_mode_is_observe_and_keeps_two_named_products(tmp_path):
    """나란히 소개된 두 상품(이름이 각각 있음)은 그대로 두 상품. 기본 모드가 observe 이고 결과에 버전·프로필이 남는다."""
    obs = observe_with([T("t1", "NH튼튼적금", "deposit_saving", "P1-R01", "NH튼튼적금"),
                        T("t2", "NH든든예금", "deposit_term", "P1-R02", "NH든든예금")],
                       {"P1-R01": {"scope": "specific", "targets": ["t1"]}, "P1-R02": {"scope": "specific", "targets": ["t2"]},
                        "P1-R03": {"scope": "specific", "targets": ["t2"]}, "P1-R04": {"scope": "shared", "targets": ["t1", "t2"]}})
    rep, d, tr = run(tmp_path, obs)
    assert rep["checks"]["passed"] and len(d["products"]) == 2
    assert d["semantic"]["classification_mode"] == "observe" and d["semantic"]["profile"] == semantic.OBSERVE_PROFILE
    assert d["semantic"]["versions"]["target_rules"] == classify.TARGET_RULES_VERSION
    assert tr["units"][0]["target_rules"] == []
    by = {r["text"]: r["product_assignment"] for r in d["regions"]}
    assert by["NH든든예금"]["product_ids"] == by["가입기간 12개월"]["product_ids"] != by["NH튼튼적금"]["product_ids"]


def test_r1_drops_unnamed_target_without_own_region(tmp_path):
    """R1: 이름이 없고 그 대상만의 영역이 없는 대상은 상품이 아니다(고정·변동 금리 표 칸을 이름 없는 대상으로 낸 g01 꼴).
    legacy(정리 없음)는 같은 응답에서 두 상품."""
    obs = observe_with([T("t1", "NH튼튼적금", "deposit_saving", "P1-R01", "NH튼튼적금"),
                        T("t2", None, "deposit_term", "P1-R03", "가입기간 12개월", "deposit_term.type")],
                       {"P1-R01": {"scope": "specific", "targets": ["t1"]}, "P1-R02": {"scope": "specific", "targets": ["t1"]},
                        "P1-R03": {"scope": "mixed", "targets": ["t1", "t2"]}, "P1-R04": {"scope": "shared", "targets": ["t1", "t2"]}})
    rep, d, tr = run(tmp_path / "o", obs)
    assert rep["checks"]["passed"] and [p["name"] for p in d["products"]] == ["NH튼튼적금"]
    assert [(a["rule"], a["tid"]) for a in tr["units"][0]["target_rules"]] == [("R1", "t2")]
    r3 = next(r for r in d["regions"] if r["text"] == "가입기간 12개월")
    assert r3["product_assignment"]["scope"] == "specific" and r3["product_assignment"]["product_ids"] == ["product_001"]
    _, legacy, _ = run(tmp_path / "l", obs, "legacy")
    assert len(legacy["products"]) == 2


def test_r2_merges_unnamed_target_into_same_template_target(tmp_path):
    """R2: 이름 없는 대상의 템플릿이 이름 있는 대상과 같으면 같은 상품(그 대상만의 영역도 합친 상품으로)."""
    obs = observe_with([T("t1", "NH튼튼적금", "deposit_saving", "P1-R01", "NH튼튼적금"),
                        T("t2", None, "deposit_saving", "P1-R03", "가입기간 12개월", "deposit_saving.monthly")],
                       {"P1-R01": {"scope": "specific", "targets": ["t1"]}, "P1-R02": {"scope": "specific", "targets": ["t1"]},
                        "P1-R03": {"scope": "specific", "targets": ["t2"]}, "P1-R04": {"scope": "shared", "targets": ["t1", "t2"]}})
    rep, d, tr = run(tmp_path, obs)
    assert rep["checks"]["passed"] and [p["name"] for p in d["products"]] == ["NH튼튼적금"]
    assert [(a["rule"], a["tid"], a["into"]) for a in tr["units"][0]["target_rules"]] == [("R2", "t2", "t1")]
    assert all(r["product_assignment"]["product_ids"] == ["product_001"] for r in d["regions"])


def test_r2_merges_two_unnamed_same_template_targets_and_keeps_failed_observation():
    """이름 없는 같은 템플릿 대상 둘(t09 꼴)은 먼저 나온 대상으로 합친다. 실패한 관측은 그대로 둔다."""
    obs = {"ok": True, "errors": [], "mentions": [], "parts": [], "missing": [],
           "targets": [{"tid": "t1", "name": None, "group": "투자성", "templates": ["pension"], "evidence": [{"feature": "pension.common"}]},
                       {"tid": "t2", "name": None, "group": "투자성", "templates": ["pension"], "evidence": [{"feature": "pension.plan_irp"}]}],
           "regions": {"P1-R01": {"scope": "specific", "targets": ["t1"], "note": None},
                       "P1-R02": {"scope": "specific", "targets": ["t2"], "note": None},
                       "P1-R03": {"scope": "mixed", "targets": ["t1", "t2"], "note": None}}}
    out = classify.apply_target_rules(obs)
    assert [t["tid"] for t in out["targets"]] == ["t1"] and len(out["targets"][0]["evidence"]) == 2
    assert out["regions"]["P1-R02"]["targets"] == ["t1"] and out["regions"]["P1-R03"] == {"scope": "specific", "targets": ["t1"],
                                                                                          "note": "R2_mixed_single"}
    assert obs["targets"][1]["tid"] == "t2"                                    # 입력은 바꾸지 않는다
    failed = {"ok": False, "errors": ["x"], "targets": [], "regions": {}, "parts": [], "missing": ["P1-R01"]}
    assert classify.apply_target_rules(failed) is failed


def test_specific_without_target_is_filled_only_when_one_target(tmp_path):
    """docs/19: 대상이 하나뿐인 관측에서 '한 상품 전용'인데 대상을 비운 영역은 그 대상으로 채운다. 대상이 둘이면 미확정 그대로."""
    one = observe_with([T("t1", "NH튼튼적금", "deposit_saving", "P1-R01", "NH튼튼적금")],
                       {"P1-R01": {"scope": "specific", "targets": ["t1"]}, "P1-R03": {"scope": "specific", "targets": []}})
    rep, d, tr = run(tmp_path / "one", one)
    r3 = next(r for r in d["regions"] if r["text"] == "가입기간 12개월")
    assert rep["checks"]["passed"] and r3["product_assignment"]["scope"] == "specific" and r3["product_assignment"]["product_ids"] == ["product_001"]
    assert tr["units"][0]["regions"]["P1-R03"]["note"] == "specific_filled_single_target"
    two = observe_with([T("t1", "NH튼튼적금", "deposit_saving", "P1-R01", "NH튼튼적금"),
                        T("t2", "NH든든예금", "deposit_term", "P1-R02", "NH든든예금")],
                       {"P1-R01": {"scope": "specific", "targets": ["t1"]}, "P1-R02": {"scope": "specific", "targets": ["t2"]},
                        "P1-R03": {"scope": "specific", "targets": []}})
    _, d2, _ = run(tmp_path / "two", two)
    assert next(r for r in d2["regions"] if r["text"] == "가입기간 12개월")["product_assignment"]["scope"] == "unknown"
