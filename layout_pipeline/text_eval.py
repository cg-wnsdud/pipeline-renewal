"""문구 평가(eval/2). 정답→출력(누락)과 출력→원본(불필요한 추가)을 따로 센다.

영역 소속 기준(eval-membership/1, 고정): 정답 줄 bbox 면적의 50% 이상이 영역 안 → 그 영역의 기대 문구,
0 초과 50% 미만 → 경계 줄(그 영역의 누락으로 세지 않음, 출력에 있어도 근거 있음), 0 → 영역 밖.

정답→출력(recall 측):
  줄마다 출력 문자열 어느 부분과 맞추는 부분 정렬(semiglobal) 편집거리를 구한다. 0 = 정규화 정확 일치,
  줄 길이 10% 이하(최소 1자) = 근사 일치, 그 밖 = 누락. 편집거리 합/정답 글자 수는 'recall 측 편집 비율'이며
  출력의 추가 글자를 세지 않으므로 표준 CER 이나 전체 출력 정확도가 아니다.
출력→원본(precision 측):
  정규화한 출력의 각 글자가 정답 줄의 3글자 조각(2글자 줄은 줄 전체)으로 뒷받침되는지 본다.
  영역 안·경계 정답으로 뒷받침되면 근거 있음, 페이지의 다른 정답으로만 뒷받침되면 위치 오류(다른 영역 문구),
  어디에도 없으면 근거 없음. 근거 없는 연속 구간이 4자 이상이면 '추가 구간'(환각·오염·통째 오독),
  3자 이하면 '글자 오류'로 센다. 정답에 없는 문구가 원본 이미지에 실제로 있을 수 있으므로(정답 누락)
  근거 없음은 오류 후보이며 원본 대조로 확인한다.
페이지 단위(evaluate_page): 페이지의 모든 정답 줄을 분모로 두고, 최종 출력의 어디에 있는지 분류한다.
"""
from __future__ import annotations

import re
import unicodedata

from .geometry import area, intersection

MEMBERSHIP = {"name": "eval-membership/1", "statement": "정답 줄 bbox 면적 50% 이상이 영역 안이면 기대 문구, "
              "0 초과 50% 미만은 경계 줄, 0 이면 영역 밖."}
EVAL_VERSION = "eval/2.1"   # 2.1: 시각 구분자 '|' 를 정규화에서 제외
FOUND_MAX_ERR = 0.10       # 줄 길이 대비 편집거리 10% 이하(최소 1자)면 근사 일치
NGRAM = 3
ADDED_RUN_MIN = 4          # 근거 없는 연속 구간이 이 길이 이상이면 '추가 구간'
DIGIT_RE = re.compile(r"\d+(?:[.,]\d+)*")
_MAP = str.maketrans({"·": "·", "•": "·", "∙": "·", "‧": "·", "ㆍ": "·", "“": '"', "”": '"', "‘": "'", "’": "'",
                      "∼": "~", "〜": "~", "～": "~", "‐": "-", "–": "-", "—": "-", "−": "-", "：": ":"})


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_MAP)
    return re.sub(r"[\s|]+", "", t.replace("​", ""))


def semiglobal(p: str, t: str) -> int:
    """p 를 t 의 어느 부분 문자열과 맞출 때의 최소 편집거리(t 의 앞뒤는 무료)."""
    if not p:
        return 0
    if p in t:
        return 0
    prev = [0] * (len(t) + 1)
    for i, pc in enumerate(p, 1):
        cur = [i] + [0] * len(t)
        for j, tc in enumerate(t, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (pc != tc))
        prev = cur
    return min(prev)


def tolerance(p: str) -> int:
    return max(1, int(len(p) * FOUND_MAX_ERR))


def digits(text: str) -> list[str]:
    return DIGIT_RE.findall(unicodedata.normalize("NFKC", text or ""))


def membership(bbox, region_bbox) -> str:
    a = area(bbox)
    r = intersection(bbox, region_bbox) / a if a else 0
    return "in" if r >= 0.5 else ("boundary" if r > 0 else "out")


def support_mask(out: str, refs: list[str]) -> list[bool]:
    """out 의 각 글자가 refs 의 3글자 조각(2글자 ref 는 전체)으로 뒷받침되는지."""
    grams = {r[i:i + NGRAM] for r in refs if len(r) >= NGRAM for i in range(len(r) - NGRAM + 1)}
    mask = [False] * len(out)
    for i in range(len(out) - NGRAM + 1):
        if out[i:i + NGRAM] in grams:
            for j in range(i, i + NGRAM):
                mask[j] = True
    for s in {r for r in refs if len(r) == 2}:
        start = out.find(s)
        while start >= 0:
            mask[start] = mask[start + 1] = True
            start = out.find(s, start + 1)
    return mask


def _runs(out: str, bad: list[bool]) -> list[str]:
    runs, cur = [], []
    for ch, b in zip(out, bad):
        if b:
            cur.append(ch)
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def precision_side(text: str | None, region_refs: list[str], page_refs: list[str]) -> dict:
    out = norm(text) if text else ""
    if not out:
        return {"output_chars": 0, "supported_chars": 0, "location_error_chars": 0, "unsupported_chars": 0,
                "added_segments": [], "char_error_runs": 0, "unsupported_rate": None}
    in_region = support_mask(out, region_refs)
    on_page = support_mask(out, page_refs)
    location = [(not a) and b for a, b in zip(in_region, on_page)]
    unsupported = [not b for b in on_page]
    runs = _runs(out, unsupported)
    return {"output_chars": len(out), "supported_chars": sum(in_region), "location_error_chars": sum(location),
            "location_error_segments": [s for s in _runs(out, location) if len(s) >= ADDED_RUN_MIN],
            "unsupported_chars": sum(unsupported),
            "added_segments": [s for s in runs if len(s) >= ADDED_RUN_MIN],
            "char_error_runs": sum(1 for s in runs if len(s) < ADDED_RUN_MIN),
            "unsupported_rate": round(sum(unsupported) / len(out), 4)}


def evaluate_region(text: str | None, region_bbox, gold_lines: list[dict]) -> dict:
    """gold_lines: 페이지 정답 줄(id, bbox, text, optional)."""
    expected, optional, boundary, outside = [], [], [], []
    for g in gold_lines:
        m = membership([float(v) for v in g["bbox"]], region_bbox)
        if m == "in":
            (optional if g.get("optional") else expected).append(g)
        elif m == "boundary":
            boundary.append(g)
        else:
            outside.append(g)
    out = norm(text) if text else ""
    rows, missing, err_sum, char_sum, exact, approx = [], [], 0, 0, 0, 0
    for g in expected:
        p = norm(g["text"])
        d = semiglobal(p, out) if out else len(p)
        status = "exact" if d == 0 else ("approx" if p and d <= tolerance(p) else "missing")
        exact += status == "exact"
        approx += status == "approx"
        err_sum += d
        char_sum += len(p)
        rows.append({"gold_id": g["id"], "text": g["text"], "edit_distance": d, "status": status,
                     "found": status != "missing"})
        if status == "missing":
            missing.append(g["id"])
    region_refs = [norm(g["text"]) for g in expected + optional + boundary]
    page_refs = [norm(g["text"]) for g in gold_lines]
    prec = precision_side(text, region_refs, page_refs)
    # 위치 오류 줄: 출력 줄이 영역 안 정답으로 설명되지 않고 영역 밖 정답 줄과 일치(줄 단위)
    inside_joined = "".join(region_refs)
    location_lines = []
    for line in (text or "").splitlines():
        n = norm(line)
        if len(n) < 4 or (inside_joined and semiglobal(n, inside_joined) <= tolerance(n)):
            continue
        for g in outside:
            p = norm(g["text"])
            if len(p) >= 4 and semiglobal(n, p) <= tolerance(n) and g["id"] not in location_lines:
                location_lines.append(g["id"])
                break
    exp_digits = [d for g in expected for d in digits(g["text"])]
    ok_digits = set(exp_digits) | {d for g in optional + boundary for d in digits(g["text"])}
    got_digits = digits(text or "")
    missing_digits = [d for d in exp_digits if exp_digits.count(d) > got_digits.count(d)]
    extra_digits = sorted({d for d in got_digits if d not in ok_digits})
    out_lines = [norm(x) for x in (text or "").splitlines() if norm(x)]
    duplicated = sorted({g["id"] for g in expected if len(norm(g["text"])) >= 4
                         and out_lines.count(norm(g["text"])) > sum(norm(x["text"]) == norm(g["text"]) for x in expected)})
    return {
        "eval_version": EVAL_VERSION, "membership": MEMBERSHIP["name"], "expected_lines": len(expected),
        "optional_lines": len(optional), "boundary_lines": len(boundary),
        "lines_exact": exact, "lines_approx_only": approx, "lines_found": exact + approx, "missing_lines": missing,
        "recall_side_edit_rate": round(err_sum / char_sum, 4) if char_sum else None,
        **prec,
        "location_error_lines": location_lines,
        "missing_digits": sorted(set(missing_digits)), "extra_digits": extra_digits,
        "duplicated_lines": duplicated, "line_detail": rows,
        "expected_text": "\n".join(g["text"] for g in expected),
    }


def _find(p: str, text_norm: str) -> str | None:
    if not p or not text_norm:
        return None
    if p in text_norm:
        return "exact"
    grams = {p[i:i + NGRAM] for i in range(max(1, len(p) - NGRAM + 1))}
    if sum(g in text_norm for g in grams) < 0.5 * len(grams):   # DP 전 저비용 거르기
        return None
    return "approx" if semiglobal(p, text_norm) <= tolerance(p) else None


def evaluate_page(gold_lines: list[dict], regions: list[dict], unassigned_texts: list[dict]) -> dict:
    """페이지의 모든 정답 줄(선택 항목 제외)을 분모로 최종 출력의 어디에 있는지 분류한다.

    regions: [{region_id, bbox, text, ocr_reference?}], unassigned_texts: [{ocr_text}] (OCR 원문, VLM 최종 문구 아님).
    분류: home(소속 영역 텍스트) exact/approx, other_region(다른 영역 텍스트, 위치 오류 또는 경계),
    unassigned_ocr_only(미배정 OCR 원문에만 있음), missing(어디에도 없음).
    정답 줄의 기하 분류: home_region 있음(50% 이상 포함 영역 존재) / no_home(레이아웃 미검출·일부 포함)."""
    texts = {r["region_id"]: norm(r.get("text") or "") for r in regions}
    unassigned = norm("".join(u.get("ocr_text") or "" for u in unassigned_texts))
    failed_ref = norm("".join("".join(r["ocr_reference"]["assigned"]) for r in regions if r.get("ocr_reference")))
    rows = []
    for g in gold_lines:
        if g.get("optional"):
            continue
        bbox = [float(v) for v in g["bbox"]]
        p = norm(g["text"])
        homes = [r["region_id"] for r in regions if membership(bbox, r["bbox"]) == "in"]
        touching = [r["region_id"] for r in regions if membership(bbox, r["bbox"]) == "boundary"]
        status, where = "missing", None
        for rid in homes:
            f = _find(p, texts[rid])
            if f:
                status, where = f"home_{f}", rid
                break
        if status == "missing":
            for rid in touching + [r["region_id"] for r in regions if r["region_id"] not in homes + touching]:
                f = _find(p, texts[rid])
                if f:
                    status, where = ("boundary_region" if rid in touching else "other_region"), rid
                    break
        if status == "missing" and _find(p, unassigned):
            status = "unassigned_ocr_only"
        if status == "missing" and _find(p, failed_ref):
            status = "failed_region_ocr_only"
        rows.append({"gold_id": g["id"], "text": g["text"], "geometry": "home" if homes else "no_home",
                     "status": status, "region_id": where})
    counts: dict = {}
    for r in rows:
        counts.setdefault(r["geometry"], {}).setdefault(r["status"], 0)
        counts[r["geometry"]][r["status"]] += 1
    return {"eval_version": EVAL_VERSION, "required_lines": len(rows), "counts": counts, "lines": rows,
            "statement": "분모 = 페이지 정답 줄 전체(선택 항목 제외). 일부 포함·미배정·레이아웃 미검출 줄도 포함."}


def similarity_cover(a: str, b: str) -> float:
    """a 의 글자가 b 에 얼마나 덮이는지(1 - 부분 정렬 편집거리/len(a)). 정규화 후 비교."""
    pa, pb = norm(a), norm(b)
    if not pa:
        return 1.0
    if not pb:
        return 0.0
    if len(pa) > 400:   # 긴 문자열은 줄 단위로 나눠 근사
        parts = [norm(x) for x in a.splitlines() if norm(x)]
        tot = sum(len(x) for x in parts)
        return 1 - sum(semiglobal(x, pb) for x in parts) / tot if tot else 1.0
    return 1 - semiglobal(pa, pb) / len(pa)
