"""layout-pipeline: 입력 → OCR 레이아웃 검출 영역 → OCR 글자 배정/미배정 → 대조 화면.

최종 영역(Region)의 출처는 `prunedResult.layout_det_res.boxes` 하나다. `overall_ocr_res` 는
판독 초안·배정 근거이며 최종 영역으로 승격하지 않는다. 자세한 계약은 docs/00-correction.md.
"""
