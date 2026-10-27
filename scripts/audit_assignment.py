"""배정 규칙 감사(모델 호출 없음). 같은 원시 응답에 엄격 포함(core_shrink=0)과 현재 규칙을 적용해 비교하고,
완화 배정(현재 규칙에서 배정됐지만 OCR bbox 가 영역 밖으로 나간 항목) 전체를 원본 crop 으로 검토 목록에 남긴다.

잉크 진단은 검토 순서를 정하는 보조 신호다. 판정은 시각 검토 파일(audit/relaxed-review.json)에 따로 기록한다.
배정 규칙을 바꾸거나 새 보정 규칙을 만들지 않는다.

사용: audit_assignment.py <run_dir> [--out <dir>]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.assignment import AssignmentRule, assign  # noqa: E402
from layout_pipeline.ink import INK_DISTANCE, ink_mask  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402
from layout_pipeline.regions import parse_view  # noqa: E402
from layout_pipeline.routing import InputView  # noqa: E402
from layout_pipeline.stage1 import resolve  # noqa: E402

TILE_W, TILE_H, LABEL_H, COLS, ROWS = 300, 130, 30, 4, 9


def strips(b, r):
    """OCR bbox 중 영역 밖 부분(좌·상·우·하 띠). 겹치는 모서리는 중복될 수 있으나 진단용이다."""
    out = []
    if b[0] < r[0]:
        out.append(("left", [b[0], b[1], min(r[0], b[2]), b[3]]))
    if b[2] > r[2]:
        out.append(("right", [max(r[2], b[0]), b[1], b[2], b[3]]))
    if b[1] < r[1]:
        out.append(("top", [b[0], b[1], b[2], min(r[1], b[3])]))
    if b[3] > r[3]:
        out.append(("bottom", [b[0], max(r[3], b[1]), b[2], b[3]]))
    return out


def ink_outside(page: np.ndarray, b, r) -> dict:
    x0, y0, x1, y1 = (int(np.floor(b[0])), int(np.floor(b[1])), int(np.ceil(b[2])), int(np.ceil(b[3])))
    box = page[y0:y1, x0:x1]
    if box.shape[0] < 5 or box.shape[1] < 5:
        return {"ink_total_px": 0, "ink_outside_px": 0, "focus": [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]}
    mask = ink_mask(box)
    outside = np.zeros_like(mask)
    rx0, ry0, rx1, ry1 = r[0] - x0, r[1] - y0, r[2] - x0, r[3] - y0
    ys, xs = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    outside |= (xs + 0.5 < rx0) | (xs + 0.5 > rx1) | (ys + 0.5 < ry0) | (ys + 0.5 > ry1)
    hit = mask & outside
    if hit.any():
        fy, fx = np.argwhere(hit).mean(axis=0)
        focus = [x0 + fx, y0 + fy]
    else:
        side = max(strips(b, r), key=lambda s: (s[1][2] - s[1][0]) * (s[1][3] - s[1][1]))[1] if strips(b, r) else b
        focus = [(side[0] + side[2]) / 2, (side[1] + side[3]) / 2]
    return {"ink_total_px": int(mask.sum()), "ink_outside_px": int(hit.sum()), "focus": [round(v, 1) for v in focus]}


def tile(page_img: Image.Image, b, r, focus, label: str, font) -> Image.Image:
    """가장 많이 돌출한 쪽 경계를 확대해 보여 준다(획이 파란 영역선을 넘는지 확인용)."""
    h = min(b[2] - b[0], b[3] - b[1])
    sides = {"left": r[0] - b[0], "top": r[1] - b[1], "right": b[2] - r[2], "bottom": b[3] - r[3]}
    side = max(sides, key=sides.get)
    fx, fy = focus
    if side in ("top", "bottom"):
        win_h = h + 16
        win_w = win_h * TILE_W / TILE_H
        edge = r[1] if side == "top" else r[3]
        fy = edge
    else:
        win_w = max(h * 1.6, 40) + 16
        win_h = win_w * TILE_H / TILE_W
        if win_h < h + 16:
            win_h = h + 16
            win_w = win_h * TILE_W / TILE_H
        fx = r[0] if side == "left" else r[2]
        fy = (b[1] + b[3]) / 2
    x0, y0 = fx - win_w / 2, fy - win_h / 2
    crop = page_img.crop((int(x0), int(y0), int(x0 + win_w), int(y0 + win_h))).convert("RGBA")
    over = Image.new("RGBA", crop.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    sx = lambda v: v - int(x0)  # noqa: E731
    sy = lambda v: v - int(y0)  # noqa: E731
    for _, s in strips(b, r):
        d.rectangle([sx(s[0]), sy(s[1]), sx(s[2]), sy(s[3])], fill=(255, 0, 0, 60))
    d.rectangle([sx(r[0]), sy(r[1]), sx(r[2]), sy(r[3])], outline=(31, 95, 191, 255), width=2)
    d.rectangle([sx(b[0]), sy(b[1]), sx(b[2]), sy(b[3])], outline=(46, 125, 50, 255), width=1)
    img = Image.alpha_composite(crop, over).convert("RGB").resize((TILE_W, TILE_H), Image.LANCZOS)
    out = Image.new("RGB", (TILE_W, TILE_H + LABEL_H), "white")
    out.paste(img, (0, LABEL_H))
    ImageDraw.Draw(out).text((3, 1), label, font=font, fill=(0, 0, 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    out = args.out or args.run / "audit"
    out.mkdir(parents=True, exist_ok=True)
    calls = {c["view_id"]: c for c in load(args.run / "calls.json") if c["ok"]}
    routes = load(args.run / "routes.json")
    pages = {p["page_id"]: p for p in load(args.run / "pages.json")}
    font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 12)
    strict, current = AssignmentRule(core_shrink=0.0), AssignmentRule()
    transitions: Counter = Counter()
    per_page = []
    relaxed = []
    review_path = out / "relaxed-review.json"
    verdicts = load(review_path)["verdicts"] if review_path.is_file() else {}
    for route in routes:
        if not route["views"]:
            continue
        regions, items = [], []
        for v in route["views"]:
            view = InputView(v["view_id"], v["page_id"], v["kind"], tuple(v["crop"]))
            raw = load(resolve(calls[v["view_id"]]["raw_path"]))["result"]["layoutParsingResults"][0]["prunedResult"]
            r, i, _ = parse_view(raw, view)
            regions += r
            items += i
        a_strict, a_cur = assign(items, regions, strict), assign(items, regions, current)
        s_status = {x["ocr_id"]: "assigned" for x in a_strict["assigned"]} | {x["ocr_id"]: x["reason"] for x in a_strict["unassigned_texts"]}
        c_status = {x["ocr_id"]: "assigned" for x in a_cur["assigned"]} | {x["ocr_id"]: x["reason"] for x in a_cur["unassigned_texts"]}
        nonempty = {i.ocr_id for i in items if not i.empty}
        page_tr = Counter((s_status[k], c_status[k]) for k in nonempty)
        transitions.update(page_tr)
        per_page.append({"page_id": route["page_id"], "nonempty": len(nonempty),
                         "strict_assigned": sum(s_status[k] == "assigned" for k in nonempty),
                         "current_assigned": sum(c_status[k] == "assigned" for k in nonempty)})
        region_by_id = {r.region_id: r for r in regions}
        page_arr = None
        page_img = None
        for a in a_cur["assigned"]:
            # 엄격 포함에서는 배정되지 않았는데 현재 규칙에서 배정된 항목 = 완화 결정 전체.
            if s_status[a["ocr_id"]] == "assigned":
                continue
            if page_arr is None:
                page_img = Image.open(pages[route["page_id"]]["image_path"]).convert("RGB")
                page_arr = np.asarray(page_img)
            r = region_by_id[a["region_id"]].bbox
            b = a["ocr_bbox"]
            ink = ink_outside(page_arr, b, r)
            # 배정 영역 안에 있지만 다른 영역과 여백이 겹친 경우: 다른 영역과의 겹침 정보를 남긴다.
            others = [c for c in next(x for x in a_strict["unassigned_texts"] if x["ocr_id"] == a["ocr_id"])["candidates"]
                      if c["region_id"] != a["region_id"]]
            sides = {k: round(v, 2) for k, v in (("left", r[0] - b[0]), ("top", r[1] - b[1]),
                                                   ("right", b[2] - r[2]), ("bottom", b[3] - r[3])) if v > 0}
            relaxed.append({"ocr_id": a["ocr_id"], "page_id": a["page_id"], "region_id": a["region_id"],
                            "ocr_text": a["ocr_text"], "empty_text": a["empty_text"], "ocr_bbox": b, "region_bbox": r,
                            "protrusion_px": a["protrusion_px"], "protrusion_sides": sides,
                            "short_side_px": round(min(b[2] - b[0], b[3] - b[1]), 1),
                            "strict_status": s_status[a["ocr_id"]], "exact_inside_assigned_region": a["exact_inside"],
                            "other_overlapping_regions": others, **ink,
                            "_img": page_img})
    relaxed.sort(key=lambda x: (-x["ink_outside_px"], -x["protrusion_px"]))
    sheets = []
    for n in range(0, len(relaxed), COLS * ROWS):
        chunk = relaxed[n:n + COLS * ROWS]
        sheet = Image.new("RGB", (COLS * TILE_W, ((len(chunk) + COLS - 1) // COLS) * (TILE_H + LABEL_H)), "white")
        for k, x in enumerate(chunk):
            idx = n + k + 1
            x["sheet_index"] = idx
            extra = f" 타영역겹침 {x['other_overlapping_regions'][0]['region_id'][-4:]}" if x["other_overlapping_regions"] else ""
            label = (f"#{idx} {x['ocr_id']}  돌출 {x['protrusion_px']}px {','.join(x['protrusion_sides'])}  "
                     f"잉크밖 {x['ink_outside_px']}px{extra}\n{x['ocr_text'][:34]}")
            t = tile(x["_img"], x["ocr_bbox"], x["region_bbox"], x["focus"], label, font)
            sheet.paste(t, ((k % COLS) * TILE_W, (k // COLS) * (TILE_H + LABEL_H)))
        path = out / f"relaxed-sheet-{len(sheets) + 1}.png"
        sheet.save(path)
        sheets.append(str(path))
    for x in relaxed:
        x.pop("_img")
        x["verdict"] = verdicts.get(x["ocr_id"], {}).get("verdict", "not_reviewed")
        x["verdict_note"] = verdicts.get(x["ocr_id"], {}).get("note")
    summary = {
        "rules": {"strict": strict.to_dict(), "current": current.to_dict()},
        "nonempty_transitions_strict_to_current": {f"{a} -> {b}": n for (a, b), n in sorted(transitions.items())},
        "per_page": per_page,
        "relaxed_total": len(relaxed),
        "relaxed_nonempty": sum(not x["empty_text"] for x in relaxed),
        "relaxed_with_ink_outside_signal": sum(x["ink_outside_px"] > 0 for x in relaxed),
        "verdict_counts": dict(Counter(x["verdict"] for x in relaxed)),
        "ink_diagnostic": {"method": "OCR bbox 테두리 2px 중앙값 색을 배경으로, RGB 거리 > %d 를 잉크로 본다" % INK_DISTANCE,
                           "use": "검토 순서용 보조 신호. 판정은 시각 검토 결과(verdict)."},
        "sheets": sheets,
    }
    dump(out / "assignment-audit.json", summary | {"relaxed": relaxed})
    print({k: v for k, v in summary.items() if k != "per_page"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
