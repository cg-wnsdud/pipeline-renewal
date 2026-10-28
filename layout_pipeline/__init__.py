"""layout-pipeline: 입력 → OCR 레이아웃 검출 영역 → OCR 글자 배정/미배정 → 대조 화면.

최종 영역(Region)의 출처는 `prunedResult.layout_det_res.boxes` 다. `overall_ocr_res` 는 판독 초안·배정 근거이며,
판독 뒤 레이아웃 상자 밖에 남은 OCR 묶음만 보충 영역(`…-S000`, supplement.py)으로 다시 읽어 더한다(2026-10-28, docs/16 §9).
자세한 계약은 docs/00-correction.md.
"""
