"""OCR 응답 → 최종 영역(Region)과 OCR 항목(OcrItem).

Region 의 출처는 `prunedResult.layout_det_res.boxes` 하나다. 원시 순번을 ID 에 고정하고 bbox 는
view offset 만 더한다(자르기·합치기·늘리기·재추정 없음). 분류명은 trace 로만 보존하며 내용을
버리거나 다른 처리를 고르는 데 쓰지 않는다. 유일한 예외는 `label == "table"` → 표 후보 표시다.

`overall_ocr_res` 는 OcrItem 으로만 만든다. Region 으로 승격하지 않는다.
`parsing_res_list` 는 서버 후처리 결과이므로 Region 과 섞지 않고 비교용 server_blocks 로만 남긴다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .routing import InputView

REGION_SOURCE = "prunedResult.layout_det_res.boxes"
OCR_SOURCE = "prunedResult.overall_ocr_res"
SERVER_BLOCK_SOURCE = "prunedResult.parsing_res_list"


class ResponseContractError(ValueError):
    pass


@dataclass
class Region:
    region_id: str
    page_id: str
    view_id: str
    source_index: int
    bbox: list[float]            # 페이지 좌표 = raw_coordinate + view offset
    raw_coordinate: list[float]  # 응답 그대로(view 좌표)
    table_candidate: bool
    raw_label: str               # trace 전용. 의미로 사용하지 않는다.
    raw_cls_id: int | None
    raw_score: float
    source: str = REGION_SOURCE

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OcrItem:
    ocr_id: str
    page_id: str
    view_id: str
    source_index: int
    ocr_text: str
    ocr_bbox: list[float]       # 페이지 좌표 = rec_boxes + view offset
    ocr_poly: list[list[float]]
    score: float
    source: str = OCR_SOURCE

    @property
    def empty(self) -> bool:
        return not self.ocr_text.strip()

    def to_dict(self) -> dict:
        return asdict(self)


def _shift_box(box, dx, dy) -> list[float]:
    return [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy]


def parse_view(pruned: dict, view: InputView) -> tuple[list[Region], list[OcrItem], list[dict]]:
    w, h = view.size
    if [pruned.get("width"), pruned.get("height")] != [w, h]:
        raise ResponseContractError(f"{view.view_id}: 응답 크기 {pruned.get('width')}x{pruned.get('height')} != {w}x{h}")
    dx, dy = view.offset
    regions = []
    for i, box in enumerate(pruned["layout_det_res"]["boxes"]):
        coord = [float(v) for v in box["coordinate"]]
        if len(coord) != 4 or coord[2] <= coord[0] or coord[3] <= coord[1]:
            raise ResponseContractError(f"{view.view_id}: layout box {i} 좌표 오류 {coord}")
        regions.append(Region(f"{view.view_id}-L{i:03d}", view.page_id, view.view_id, i, _shift_box(coord, dx, dy),
                              coord, box.get("label") == "table", box.get("label"), box.get("cls_id"),
                              float(box.get("score"))))
    raw = pruned["overall_ocr_res"]
    texts, boxes, scores, polys = raw["rec_texts"], raw["rec_boxes"], raw["rec_scores"], raw["rec_polys"]
    if not (len(texts) == len(boxes) == len(scores) == len(polys)):
        raise ResponseContractError(f"{view.view_id}: rec_* 길이 불일치 {len(texts)}/{len(boxes)}/{len(scores)}/{len(polys)}")
    items = [OcrItem(f"{view.view_id}-T{i:03d}", view.page_id, view.view_id, i, text,
                     _shift_box([float(v) for v in box], dx, dy), [[p[0] + dx, p[1] + dy] for p in poly], float(score))
             for i, (text, box, score, poly) in enumerate(zip(texts, boxes, scores, polys))]
    blocks = [{"source": SERVER_BLOCK_SOURCE, "view_id": view.view_id, "block_id": b.get("block_id"),
               "block_label": b.get("block_label"), "bbox": _shift_box([float(v) for v in b["block_bbox"]], dx, dy)}
              for b in (pruned.get("parsing_res_list") or [])]
    return regions, items, blocks
