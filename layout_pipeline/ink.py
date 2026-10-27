"""OCR 상자 안 글자 잉크 측정(배정 감사·ink-containment/1 배정 규칙 공용). 상자·영역 bbox 는 바꾸지 않는다."""
from __future__ import annotations

import numpy as np

INK_DISTANCE = 80        # OCR 상자 테두리 2px 중앙값 색(배경)과의 RGB 거리. 이보다 크면 잉크로 본다.


def ink_mask(arr: np.ndarray) -> np.ndarray:
    """OCR bbox 테두리 2px 의 중앙값 색을 배경으로 보고 거리가 큰 픽셀을 잉크로 본다."""
    ring = np.concatenate([arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3), arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    return np.sqrt(((arr.astype(float) - bg) ** 2).sum(axis=2)) > INK_DISTANCE


def outside_ink(page: np.ndarray, b, r) -> dict:
    """OCR 상자 b 의 잉크 중 영역 r 밖에 있는 픽셀 수와 영역선에서 가장 먼 거리(px)."""
    x0, y0, x1, y1 = (int(np.floor(b[0])), int(np.floor(b[1])), int(np.ceil(b[2])), int(np.ceil(b[3])))
    box = page[max(0, y0):y1, max(0, x0):x1]
    x0, y0 = max(0, x0), max(0, y0)
    if box.shape[0] < 5 or box.shape[1] < 5:
        return {"ink_total_px": 0, "ink_outside_px": 0, "ink_outside_max_px": 0.0, "hit": None, "origin": (x0, y0)}
    mask = ink_mask(box)
    ys, xs = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    px, py = xs + x0 + 0.5, ys + y0 + 0.5
    hit = mask & ((px < r[0]) | (px > r[2]) | (py < r[1]) | (py > r[3]))
    dist = 0.0
    if hit.any():
        d = np.maximum.reduce([r[0] - px, px - r[2], r[1] - py, py - r[3]])
        dist = round(float(d[hit].max()), 1)
    return {"ink_total_px": int(mask.sum()), "ink_outside_px": int(hit.sum()), "ink_outside_max_px": dist,
            "hit": hit, "origin": (x0, y0)}
