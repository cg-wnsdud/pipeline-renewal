"""문구 정규화·비교 도구. 판독 검사(OCR 대조·숫자 비교·상자 밖 문구·재판독 선택·보충 중복 판정)가 함께 쓴다.

- norm: NFKC·기호 통일 후 공백과 시각 구분자 '|' 를 지운 비교용 문자열.
- semiglobal: 한 문자열을 다른 문자열의 어느 부분과 맞출 때의 최소 편집거리(앞뒤 무료).
- tolerance: 줄 길이 10% 이하(최소 1자)를 근사 일치로 보는 편집거리 허용치.
- similarity_cover: a 의 글자가 b 에 덮이는 비율.
"""
from __future__ import annotations

import re
import unicodedata

FOUND_MAX_ERR = 0.10       # 줄 길이 대비 편집거리 10% 이하(최소 1자)면 근사 일치
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
