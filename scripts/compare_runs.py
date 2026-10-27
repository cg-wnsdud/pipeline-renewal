"""검출 설정 비교(모델 호출 없음). 같은 고정 기하 기준·배정 규칙으로 실행 폴더들을 페이지별로 나란히 놓는다.

함께 보는 항목: 정답 줄 기하 포함 분포, 배정 정책 단일 배정률, 큰 컨테이너, 표 후보 유지/소실,
두 상품 구역을 섞는 영역(gold-relations.json 의 단일 상품 구역 기준), 원시 중첩. 원본 위 비교 이미지도 만든다.

사용: compare_runs.py --run 이름=폴더 [--run ...] --pages p1,p2 --out <dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from layout_pipeline.geometry import area, contains, intersection  # noqa: E402
from layout_pipeline.jsonio import dump, load  # noqa: E402

CONTAINER_MIN_CHILDREN = 2       # 다른 영역을 2개 이상 완전히 포함하는 영역 = 컨테이너(진단용 표시 기준)
LARGE_AREA_FRAC = 0.25           # 페이지 면적 25% 이상 영역 = 큰 영역(진단용 표시 기준)
MIX_FRAC = 0.10                  # 영역 면적의 10% 이상이 서로 다른 단일 상품 구역에 각각 걸치면 혼합


def product_zones(page_id: str) -> list[dict]:
    rel = load(ROOT / "fixtures" / "gold-relations.json")
    return [z for s in rel["submissions"].values() for z in s["zones"] if z["page"] == page_id and len(z["expect"]) == 1]


def page_summary(run: Path, page_id: str, base_tables: list[list[float]] | None) -> dict:
    rec = load(run / "records" / f"{page_id}.json")
    page = rec["page"]
    regions = rec["regions"]
    boxes = {r["region_id"]: r["bbox"] for r in regions}
    page_area = page["width"] * page["height"]
    containers = []
    for r in regions:
        kids = [o for o in regions if o is not r and contains(r["bbox"], o["bbox"])]
        if len(kids) >= CONTAINER_MIN_CHILDREN:
            containers.append({"region_id": r["region_id"], "label": r["raw_label"], "children": len(kids),
                               "area_frac": round(area(r["bbox"]) / page_area, 3)})
    large = [r["region_id"] for r in regions if area(r["bbox"]) / page_area >= LARGE_AREA_FRAC]
    zones = product_zones(page_id)
    mixed = []
    for r in regions:
        hits = {z["expect"][0] for z in zones if intersection(r["bbox"], z["bbox"]) / area(r["bbox"]) >= MIX_FRAC}
        if len(hits) >= 2:
            mixed.append(r["region_id"])
    tables = [r["bbox"] for r in regions if r["table_candidate"]]

    def iou(a, b):
        i = intersection(a, b)
        return i / (area(a) + area(b) - i) if i else 0.0
    kept = None if base_tables is None else sum(any(iou(t, b) >= 0.5 for t in tables) for b in base_tables)
    gold = rec.get("gold") or {}
    nonempty_un = [u for u in rec["unassigned_texts"] if not u["empty_text"]]
    reasons = {}
    for u in nonempty_un:
        key = "ambiguous" if u["reason"].startswith("ambiguous") else u["reason"]
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "regions": len(regions), "table_candidates": len(tables),
        "base_tables_kept": kept, "base_tables": None if base_tables is None else len(base_tables),
        "containers": containers, "large_regions": large, "mixed_product_regions": mixed,
        "overlap_pairs": len(rec["diagnostics"]["region_overlaps"]),
        "gold_required": gold.get("required_lines"),
        "gold_geometry": (gold.get("geometry") or {}).get("counts"),
        "gold_single_assign": (gold.get("assignment_policy") or {}).get("counts", {}).get("covered"),
        "unassigned_nonempty": reasons,
        "gold_tables": [(t["gold_table"], t["status"]) for t in (gold.get("tables") or {}).get("gold_tables", [])],
        "_boxes": boxes, "_tables": tables, "_image": page["image_path"],
        "_unassigned": [u["ocr_bbox"] for u in nonempty_un if u["reason"] == "outside_layout"],
    }


def panel(s: dict, title: str, width: int, font) -> Image.Image:
    im = Image.open(s["_image"]).convert("RGB")
    d = ImageDraw.Draw(im)
    lw = max(2, im.width // 500)
    for b in s["_unassigned"]:
        d.rectangle(b, outline=(194, 24, 91), width=lw)
    for rid, b in s["_boxes"].items():
        is_table = b in s["_tables"]
        color = (198, 40, 40) if is_table else (31, 95, 191)
        d.rectangle(b, outline=color, width=lw * (2 if rid in s["large_regions"] else 1))
    for c in s["containers"]:
        b = s["_boxes"][c["region_id"]]
        d.rectangle(b, outline=(230, 160, 0), width=lw * 3)
    scale = width / im.width
    im = im.resize((width, int(im.height * scale)), Image.LANCZOS)
    out = Image.new("RGB", (width, im.height + 44), "white")
    out.paste(im, (0, 44))
    g = s["gold_geometry"] or {}
    ImageDraw.Draw(out).text((4, 2), f"{title}\n영역 {s['regions']} 표 {s['table_candidates']} 컨테이너 {len(s['containers'])} "
                             f"단일 {g.get('single', 0)}/{s['gold_required']}", font=font, fill=(0, 0, 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="이름=폴더 (첫 번째가 기준)")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    runs = [(n, Path(p)) for n, p in (x.split("=", 1) for x in args.run)]
    pages = args.pages.split(",")
    font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 16)
    result = {"criteria": {"geometry": "exact-containment/1(고정)", "assignment": "core-containment/1 core_shrink=0(엄격)",
                           "container": f"다른 영역을 {CONTAINER_MIN_CHILDREN}개 이상 완전히 포함",
                           "large": f"페이지 면적 {LARGE_AREA_FRAC:.0%} 이상",
                           "mixed_product": f"영역 면적 {MIX_FRAC:.0%} 이상이 서로 다른 단일 상품 구역에 각각 걸침",
                           "table_kept": "기준 실행의 표 후보와 IoU≥0.5 인 표 후보 존재"},
              "runs": {n: str(p) for n, p in runs}, "pages": {}}
    args.out.mkdir(parents=True, exist_ok=True)
    for pid in pages:
        base = page_summary(runs[0][1], pid, None)
        rows = {runs[0][0]: base}
        for name, path in runs[1:]:
            rows[name] = page_summary(path, pid, base["_tables"])
        panels = [panel(s, n, 520, font) for n, s in rows.items()]
        sheet = Image.new("RGB", (520 * len(panels) + 8 * (len(panels) - 1), max(p.height for p in panels)), "white")
        for i, p in enumerate(panels):
            sheet.paste(p, (i * 528, 0))
        sheet.save(args.out / f"{pid}.png")
        result["pages"][pid] = {n: {k: v for k, v in s.items() if not k.startswith("_")} for n, s in rows.items()}
    dump(args.out / "comparison.json", result)
    lines = ["| page | 조건 | 영역 | 표 후보(기준 유지) | 컨테이너 | 큰 영역 | 상품 혼합 | 정답 기하: 단일/복수/일부/미포함 | 단일 배정 | 미배정(밖/부분/모호) |",
             "|---|---|---:|---|---:|---:|---:|---|---:|---|"]
    for pid, rows in result["pages"].items():
        for n, s in rows.items():
            g, u = s["gold_geometry"] or {}, s["unassigned_nonempty"]
            kept = "" if s["base_tables_kept"] is None else f" ({s['base_tables_kept']}/{s['base_tables']})"
            lines.append(f"| {pid} | {n} | {s['regions']} | {s['table_candidates']}{kept} | {len(s['containers'])} | "
                         f"{len(s['large_regions'])} | {len(s['mixed_product_regions'])} | "
                         f"{g.get('single', 0)}/{g.get('multiple', 0)}/{g.get('partial', 0)}/{g.get('none', 0)} (총 {s['gold_required']}) | "
                         f"{s['gold_single_assign']} | {u.get('outside_layout', 0)}/{u.get('partial_overlap', 0)}/{u.get('ambiguous', 0)} |")
    (args.out / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
