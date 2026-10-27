"""영역 단위 작은 정답(region-gold/1, fixtures/new-inputs-gold.json) 평가(평가 전용, docs/11 §4).

영역마다 따로 센다. 합산 점수 하나로 판정하지 않는다.
- 문구 보존: 글자·숫자·%만 남긴 키(structure_eval.content)로 정답 줄이 출력에 있는지(편집거리 10% 이하), 출력에서 정답 어디에도
  없는 연속 4자 이상 구간(추가·유입).
- 줄 순서: 찾은 정답 줄의 출력 위치가 정답 순서대로인지.
- 원문 표기: 문장부호·기호 차이(공백 제외 정규화 키의 편집거리 − 내용 키 편집거리)와 공백 유무 차이. 공백 개수, 기호 옆 공백,
  정답의 줄바꿈 자리, 따옴표 모양은 평가하지 않는다(픽셀 간격으로 단정할 수 없음).
- 표: is_table, 격자 크기, 셀(위치·span·문구) 일치, 행 혼합(다른 행 칸의 문구가 섞인 행), relations 조건.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from .structure_eval import content
from .text_eval import semiglobal, support_mask, tolerance

MARKERS = set("■▪○①②③④⑤※·-•ㆍ")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "∼": "~", "～": "~", "ㆍ": "·", "•": "·", "：": ":"})


def sym_key(text: str) -> str:
    """공백만 뺀 표기 키(문장부호·기호 보존, '|' 도 남김)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).replace("​", "")
    return re.sub(r"\s+", "", t)


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _space_flags(text: str) -> tuple[str, list[str]]:
    """공백을 뺀 글자열과 각 글자 뒤의 공백 표지(' ' 있음, '' 없음, '?' 판정 안 함: 줄바꿈·기호 옆)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).replace("​", "")
    chars, flags = [], []
    for k, ch in enumerate(t):
        if ch.isspace():
            if flags:
                flags[-1] = "?" if (ch == "\n" or flags[-1] == "?") else " "
            continue
        if flags and flags[-1] == "" and (ch in MARKERS or chars[-1] in MARKERS):
            flags[-1] = "?"
        if flags and flags[-1] == " " and (ch in MARKERS or chars[-1] in MARKERS):
            flags[-1] = "?"
        chars.append(ch)
        flags.append("")
    return "".join(chars), flags


def spacing_errors(gold: str, out: str) -> list[str]:
    """두 글자 모두 맞춰진(연속 일치) 자리에서 공백 유무가 다른 곳. 출력의 줄바꿈도 판정하지 않는다."""
    g, gf = _space_flags(gold)
    o, of = _space_flags(out)
    errs = []
    for blk in difflib.SequenceMatcher(None, g, o, autojunk=False).get_matching_blocks():
        for k in range(blk.size - 1):
            a, b = gf[blk.a + k], of[blk.b + k]
            if "?" in (a, b):
                continue
            if a != b:
                errs.append(g[max(0, blk.a + k - 3):blk.a + k + 1] + ("␣" if a else "∅") + g[blk.a + k + 1:blk.a + k + 4])
    return errs


def _pos(p: str, t: str) -> int | None:
    """p 가 t 에서 편집거리 허용 안에 맞는 끝 위치(근사)."""
    if not p:
        return None
    i = t.find(p)
    if i >= 0:
        return i
    if semiglobal(p, t) > tolerance(p):
        return None
    best, best_d = None, None
    for start in range(0, max(1, len(t) - len(p) + 1)):
        d = lev(p, t[start:start + len(p)])
        if best_d is None or d < best_d:
            best, best_d = start, d
    return best


def evaluate_text(gold: dict, text: str | None) -> dict:
    lines = gold["lines"]
    opt = gold.get("optional") or []
    out = text or ""
    co = content(out)
    found, missing = [], []
    order_ok, last, char_err = True, 0, 0
    for ln in lines:
        key = content(ln)
        # 같은 문구가 여러 번 나오는 경우(표의 같은 금리 등) 앞 줄 위치 뒤에서 먼저 찾는다
        p = _pos(key, co[last:])
        if p is not None:
            p += last
        else:
            p = _pos(key, co)
            if p is not None:
                order_ok = False
        if p is None:
            missing.append(ln)
            continue
        found.append(ln)
        char_err += min(semiglobal(key, co[p:p + len(key) + tolerance(key)]), semiglobal(key, co))
        last = max(last, p + 1)
    refs = [content(x) for x in lines + opt]
    mask = support_mask(co, refs)
    runs, cur = [], ""
    for ch, ok in zip(co, mask):
        if not ok:
            cur += ch
        elif cur:
            runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    added = [r for r in runs if len(r) >= 4]
    gj = "\n".join(lines)
    sym_extra = max(0, lev(sym_key(gj), sym_key(out)) - lev(content(gj), co)) if out and len(sym_key(gj)) < 600 else None
    allowed = set(sym_key("".join(lines + opt))) | MARKERS
    stray = sorted({ch for ch in sym_key(out) if ch not in allowed})
    sp = [] if gold.get("spacing_uncertain") or not out else spacing_errors(gj, out)
    return {"missing_lines": missing, "lines": len(lines), "found": len(found), "order_ok": order_ok,
            "char_errors": char_err, "added_segments": added, "symbol_errors": sym_extra, "stray_chars": stray,
            "spacing_errors": sp, "content_exact": content(gj) == co}


def _cell_at(cells: list[dict], row: int, col: int) -> dict | None:
    for c in cells:
        if c["row"] <= row < c["row"] + c["rowspan"] and c["col"] <= col < c["col"] + c["colspan"]:
            return c
    return None


def _has(a: str, b: str) -> bool:
    return bool(content(b)) and content(b) in content(a)


def rows_mixed(gold_cells: list[dict], rows: list[list[str]] | None) -> list[int]:
    """정답 각 행에 대해, 그 행의 시작 칸 문구를 모두 담고 다른 행 시작 칸 문구(병합 칸 제외)를 담지 않은 출력 행이 없으면 그 행 번호."""
    if not rows:
        return sorted({c["row"] for c in gold_cells})
    out_rows = [" ".join(r) for r in rows]
    bad = []
    for r in sorted({c["row"] for c in gold_cells}):
        need = [c["text"] for c in gold_cells if c["row"] == r and content(c["text"])]
        covering = {id(c) for c in gold_cells if c["row"] <= r < c["row"] + c["rowspan"]}
        # 이 행 칸 문구의 일부인 다른 행 문구('일시상환/혼합상환' ⊂ '일시상환/혼합상환/마이너스통장')는 혼합 근거로 쓰지 않는다
        foreign = [c["text"] for c in gold_cells if c["row"] != r and id(c) not in covering and len(content(c["text"])) >= 6
                   and not any(content(c["text"]) in content(x) for x in need)]
        ok = any(all(_pos(content(x), content(o)) is not None for x in need)
                 and not any(_pos(content(f), content(o)) is not None for f in foreign) for o in out_rows)
        if not ok:
            bad.append(r)
    return bad


def evaluate_table(gold_t: dict, table: dict | None) -> dict:
    status = (table or {}).get("status")
    res = {"gold_is_table": gold_t["is_table"], "status": status}
    if not gold_t["is_table"]:
        res["ok"] = status == "not_table"
        return res
    cells = (table or {}).get("cells")
    rows = (table or {}).get("rows")
    gcells = gold_t["cells"]
    res["rows_mixed"] = rows_mixed(gcells, rows)
    if cells is not None:
        res["dims_ok"] = [table.get("n_rows"), table.get("n_cols")] == [gold_t["n_rows"], gold_t["n_cols"]]
        exact = 0
        for g in gcells:
            o = next((c for c in cells if (c["row"], c["col"], c["rowspan"], c["colspan"]) ==
                      (g["row"], g["col"], g["rowspan"], g["colspan"])), None)
            exact += bool(o) and semiglobal(content(g["text"]), content(o["text"])) <= tolerance(content(g["text"])) \
                and abs(len(content(g["text"])) - len(content(o["text"]))) <= tolerance(content(g["text"]))
        res["cells_exact"] = exact
        res["cells_gold"] = len(gcells)
    rel = {}
    for r in gold_t.get("relations") or []:
        if cells is None and r["check"] in ("dims", "span", "span_not_cover"):
            rel[r["id"]] = None          # 문자열 rows 는 병합 범위·격자를 표현하지 못한다(실패로 세지 않고 따로 표시)
        elif r["check"] == "dims":
            rel[r["id"]] = table.get("n_cols") == r["n_cols"] and \
                all(_has((_cell_at(cells, 0, k) or {}).get("text", ""), h) for k, h in enumerate(r["header"]))
        elif r["check"] == "span":
            c = None if cells is None else next((x for x in cells if _has(x["text"], r["text"])), None)
            rel[r["id"]] = bool(c) and (c["row"], c["col"], c["rowspan"], c["colspan"]) == (r["row"], r["col"], r["rowspan"], r["colspan"])
        elif r["check"] == "same_row":
            src = [" ".join(x) for x in rows or []]
            rel[r["id"]] = all(any(_has(o, a) and _has(o, b) for o in src) for a, b in r["pairs"])
        elif r["check"] == "not_in_cell":
            if cells is not None:
                hit = [x for x in cells if _has(x["text"], r["cell_text"])]
                rel[r["id"]] = bool(hit) and not any(_has(x["text"], r["forbidden"]) for x in hit)
            else:
                hit = [" ".join(x) for x in rows or [] if any(_has(c, r["cell_text"]) for c in x)]
                rel[r["id"]] = bool(hit) and not any(_has(c, r["forbidden"]) for x in rows or []
                                                     for c in x if _has(c, r["cell_text"]))
        elif r["check"] == "span_not_cover":
            c = None if cells is None else next((x for x in cells if _has(x["text"], r["text"])), None)
            rel[r["id"]] = bool(c) and not (c["row"] <= r["row"] < c["row"] + c["rowspan"])
    res["relations"] = rel
    res["structure_not_expressible"] = sorted(k for k, v in rel.items() if v is None)
    res["ok"] = status == "table" and not res["rows_mixed"] and all(v for v in rel.values() if v is not None) and \
        (cells is None or (res["dims_ok"] and res["cells_exact"] == res["cells_gold"]))
    return res


def evaluate(gold: dict, result: dict) -> dict:
    regions = {g["region_id"]: g for g in result["regions"]}
    out = {}
    for rid, g in gold["regions"].items():
        r = regions.get(rid)
        if r is None:
            continue
        e = evaluate_text(g, r["text"])
        if g.get("table"):
            e["table"] = evaluate_table(g["table"], r.get("table"))
        e["quality"] = r["review"]["quality"]
        e["context"] = r["review"]["context"]
        e["has_error"] = bool(e["missing_lines"] or not e["order_ok"] or e["char_errors"] or e["added_segments"]
                              or e["stray_chars"] or (e.get("table") and not e["table"]["ok"]))
        out[rid] = e
    return out
