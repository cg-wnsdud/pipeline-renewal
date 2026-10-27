"""축 정렬 상자(x0, y0, x1, y1) 계산. 상자를 늘리거나 합치는 함수는 두지 않는다."""
from __future__ import annotations


def area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersection(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def protrusion(inner, outer) -> float:
    """inner 가 outer 밖으로 가장 많이 나간 변의 길이(px). 완전히 안이면 0."""
    return max(0.0, outer[0] - inner[0], outer[1] - inner[1], inner[2] - outer[2], inner[3] - outer[3])


def core(b, shrink: float) -> list[float]:
    """각 변을 짧은 변 × shrink 만큼 안쪽으로 줄인 상자(배정 판정 전용, 저장 bbox 아님)."""
    t = shrink * min(b[2] - b[0], b[3] - b[1])
    return [b[0] + t, b[1] + t, b[2] - t, b[3] - t]
