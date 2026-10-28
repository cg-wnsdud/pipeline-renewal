# 00. 구현 방향 교정과 데이터 계약

- **v1 (2026-10-24)**: 최초 교정.
- **v2 (2026-10-24 후속)**: `claude-next-stage-prompt.md` 반영.
  - 배정 기준을 엄격 포함으로 변경
  - 지표 분리
  - 레이아웃 요청 옵션 잠정 채택
  - VLM 계약 구체화
  - `boundary-plan/2`

`new-pipeline/`은 비교 근거로 보존한다. 파일을 삭제하거나 되돌리지 않았다. 이 폴더는 그 코드를 import하지 않는다.

## 1. 새 실행 경로에서 제외한 기존 경로

| 기존 위치(new-pipeline) | 제외한 동작 | 이 폴더의 대응 |
|---|---|---|
| `parser_next/views.py`, `pipeline.py` | VLM 2,580,480px 예산으로 OCR 입력을 band 분할. A4·정사각형·일반 직사각형도 균등 band | `routing.py`: 일반 페이지는 전체 이미지 OCR 1회. VLM 예산은 VLM 입력의 서버 리사이즈 추정에만 쓴다 |
| `parser_next/ocr.py` | `overall_ocr_res.rec_boxes`를 최종 Region으로 사용 | `regions.py`: Region = `layout_det_res.boxes`. rec_*는 `OcrItem`(판독 참고·배정 근거)으로만 쓴다 |
| `parser_next/reading.py` | OCR 줄 crop을 번호 붙은 줄 시트로 재배치 | 없음. VLM 입력은 원래 페이지 배치(+표시 사본) 또는 검출 영역 전체 crop만 쓴다 |
| `parser_next/relations.py` | 페이지의 모든 OCR 줄에 표 번호·행·열 지정 | 없음. 표 확인은 OCR `table` 후보 영역에만 한다 |
| 여러 곳 | 문자열 병합, bbox 합집합, 줄 단위 fallback, 문제별 재크롭 | 없음. 미배정 OCR은 `unassigned_texts`로 따로 보존한다 |

기존 코드에서 옮긴 것은 `layout_pipeline/hwp_bridge.py`(HWP/HWPX→PDF 변환 연결) 하나다.

## 2. 입력 경로

1. 페이지마다 다음을 기록한다: 가로·세로·비율·방향, 형태(`square|a4|general|long`).
2. `square`·`a4`·`general` → `whole`: 페이지 PNG 바이트를 그대로 OCR에 1회 보낸다. 보낸 해시와 응답 크기를 호출마다 대조한다.
3. 비율 분기값 `long_aspect_min=3.0`은 **잠정 라우팅값**이다.
   - 현재 자료 비율의 빈 구간에 둔 값이다.
   - 모델 최대 비율도 아니고 검증된 값도 아니다.
   - A4·정사각형은 설정과 무관하게 whole이다(`load_policy` 검사).
4. 해상도 조건은 분기와 분리한다.
   - OCR: 서버 `text_det_params`와 검출 배율 추정치를 기록만 한다.
   - VLM: 이미지마다 서버 리사이즈 추정치를 기록한다(`vlm_client.server_resize`).
5. 레이아웃 요청 옵션: `config/layout-options.json` = `{"layoutMergeBboxesMode": "large"}`(**잠정**).
   - 근거와 탈락 조건은 docs/02 2절에 있다.
   - class별 threshold dict는 확인 호출에서 의도대로 적용되지 않았다(기본값 dict → 0.5 미만 제목 3개 소실). 공개 소스의 int 키 조회와 JSON 문자열 키 불일치 가설과 부합하지만 실행 컨테이너 코드는 확인하지 않았으므로 원인은 미확정이다. 서버 확인 전에는 보내지 않는다(확인 요청: Codex `server-check-request.md`).

### 긴 이미지: boundary-plan/2

```json
{"contract": "boundary-plan/2", "plan_version": "...", "page_id": "f08_p01",
 "page_image_sha256": "<필수>", "page_size": [667, 6130], "coordinate_system": "page_px_xyxy", "axis": "y",
 "policy": {"name": "...", "version": "...", "max_tile_extent_px": 1668, "max_tile_aspect": 2.5,
            "status": "experimental", "note": "서버 보장값이 아닌 검증 대상 정책값"},
 "status": "validated | candidate | no_safe_boundary | oversized_region",
 "cuts": [0, ..., 6130],
 "boundaries": [{"position": 1668, "evidence": {...},
                 "semantic_check": {"method": "human_review", "verified_by": "...", "result": "safe"}}]}
```

- **`validated` 조건**: 아래가 모두 맞아야 실행한다. 하나라도 어긋나면 `BOUNDARY_PLAN_INVALID`(실행 안 함).
  - 필수 필드가 모두 있고, 해시·크기·좌표계가 일치한다.
  - cuts가 0부터 전체 길이까지 겹침 없이 정수로 증가한다.
  - **모든 타일이 선언한 정책 한도 안에 있다**(전체 경계열 검사).
  - 내부 cut마다 근거가 있다.
  - 의미 검증 방식이 승인 목록(현재 `human_review`만)에 있고 결과가 `safe`다.
- **VLM 판정**: `candidate`로만 저장한다.
  - 경계 VLM 12회 실험에서 제목-설명 사이를 반복해서 안전으로 오판했다.
  - 다수결, `uncertain=false`, 문자열 `validated`는 의미 검증을 대신하지 않는다.
- **`compose_sequence`**: 검증된 안전 위치만으로 한도 안의 연결된 경계열이 있는지 판정한다. 경계별로 골라 단순히 이어 붙이지 않는다.
  - 경계열을 만들 수 없으면 `no_safe_boundary`.
  - 한도보다 긴 보호 구간이 있으면 `oversized_region`.
- `candidate`·`no_safe_boundary`·`oversized_region`은 실행하지 않고, 대조 화면에 후보선과 근거를 표시한다.
- 최종 타일 OCR에서 내부 경계에 닿은 영역은 `TILE_EDGE_REGION` 이슈가 된다. 페이지 품질 상태 `review_required`에 반영한다.

## 3. 데이터 단위와 상태

> **변경(2026-10-28, 사용자 결정, docs/16 §9)**: 영역 출처가 레이아웃 상자 하나라는 계약을 '레이아웃 상자 + 보충 영역'으로 바꿨다. 레이아웃 상자 영역(`-L`)의 ID·bbox 는 그대로이고, 상자가 놓친 글자만 보충 영역(`-S`)으로 더한다(`--no-supplement` 로 끌 수 있음).

| 단위 | 출처 | 필드 |
|---|---|---|
| Page | 이미지 원본(EXIF 반영) 또는 PDF/HWP 렌더 | `page_id, width, height, geometry, image_sha256, source` |
| InputView | 경로 결정 | `view_id, kind(whole|tile), crop(페이지 좌표)` |
| Region | `prunedResult.layout_det_res.boxes[i]` | `region_id = {view_id}-L{i:03d}`, `bbox = coordinate + offset`, `raw_coordinate`, `table_candidate`, trace용 `raw_label/raw_score` |
| 보충 Region(2026-10-28, docs/16 §9) | 판독 뒤 레이아웃 상자 밖에 남은 OCR 묶음, 또는 같은 view 를 layoutThreshold 0.3 으로 다시 받은 `table` 상자 | `region_id = {view_id}-S{k:03d}`, `source = supplement:ocr_gap|supplement:table_threshold`, 원본에서 다시 읽고 기존 영역과 중복이 아닐 때만 추가. 근거 `trace/supplement.json` |
| OcrItem | `prunedResult.overall_ocr_res.rec_*[j]` | `ocr_id = {view_id}-T{j:03d}`, `ocr_text, ocr_bbox, ocr_poly, score` |
| server_blocks | `parsing_res_list` | 비교 전용 |

- **페이지 상태는 둘로 나눈다.**
  - `status`(실행): `READY`, `BOUNDARY_*`, `OCR_FAILED` 등.
  - `quality`(내용): `not_verified`, `review_required`, `not_processed`.
- READY는 실행이 끝났다는 뜻일 뿐이다. 품질 통과로 표시하는 단계는 아직 없다.

## 4. OCR 배정 기준 — 엄격 포함(core-containment/1, core_shrink=0)

- **배정 조건**: 원래 OCR bbox 전체가 한 영역 안에 있고, 다른 영역과 겹치지 않아야 배정한다. 그 외는 `unassigned_texts`로 보낸다.
  - 사유: `outside_layout`, `partial_overlap`, `ambiguous_multiple_regions`, `ambiguous_overlapping_regions`, `invalid_bbox`.
  - 완화 규칙을 쓸 때는 `margin_overlap_only`도 있다. 중심부는 영역 밖이고 bbox 여백만 걸친 경우로, `outside_layout`과 구분한다.
- **0.25 완화 규칙을 기본에서 뺀 이유**: 완화 배정 177개 전체를 확대 원본으로 감사했다(`work/audit/assignment-base`, Claude 시각 검토).
  - 글자 획이 영역 밖으로 나감: 39개
  - 판단 불가: 30개
  - 여백만 걸침: 108개
  - 지시("실제 글자가 영역 밖이거나 판단 불가하면 미배정")에 따라 엄격 포함으로 되돌렸다. 0.25는 `--core-shrink 0.25` 비교용으로만 남긴다.
- **중심점 하나로는 판정하지 않는다.** 저장 bbox는 어느 규칙에서도 바뀌지 않는다.
- **미배정 항목 필드**: `ocr_id, source_index, page_id, view_id, ocr_text, ocr_bbox, score, reason, candidates(포함비·돌출)`, `source="prunedResult.overall_ocr_res"`, `review_required=true`, `final=false`.
- **`verify_partition`**: 모든 OCR 항목이 배정과 미배정 중 정확히 한 곳에 한 번만 있는지 검사한다. 실패하면 예외를 낸다.
- **보고서의 두 지표는 섞지 않는다.**
  - 영역 검출: 고정 기하 기준 `exact-containment/1`. 정답 줄이 원시 bbox에 미포함/일부/단일/복수 중 어디에 해당하는지.
  - 배정 정책: 단일 배정률.

## 5. VLM 계약(`vlm_contract.py`, `vlm_tasks.py`, `vlm_client.py`)

- **요청 단위**
  - Region은 논리 결과 단위다. HTTP 호출 하나에 여러 Region을 묶는다.
  - A: 원본 페이지 + 대상 영역을 표시한 별도 사본 + ID/bbox/초안을 페이지당 1회 보낸다.
  - B: 검출 영역 전체 crop 여러 장 + 이미지별 ID를 보낸다.
  - 줄 시트는 금지한다.
- **표 후보**: `table_check`는 OCR `table` 후보에만 보낸다. crop 1장으로 `is_table`(boolean), 전체 문구, 행·셀 대응을 함께 받는다.
  - false여도 같은 bbox와 문구를 유지한다.
  - 비후보 영역에는 표 전용 요청을 만들지 않는다.
- **계약 검사**: HTTP 상태, `finish_reason=stop`(아니면 잘림), JSON 파싱, 결과 Region ID의 누락·중복·추가 금지, 타입.
  - `text`는 string 또는 null이고, `uncertain`·`is_table`은 실제 boolean이어야 한다. `bool()`로 강제 변환하지 않는다.
- **null과 빈 문자열을 구분한다**: `text=null`은 판독 실패, `""`은 글자 없음.
- **needs_review는 여러 근거를 합쳐 정한다**(`review_reasons`).
  - 품질 근거: 계약 오류, 판독 실패, uncertain, 빈 출력 vs OCR, OCR↔VLM 상호 덮임 0.85 미만(잠정), 숫자 불일치.
  - 맥락 근거: 원시 중첩, 경계에 걸친 OCR, OCR 참고 없음, 표 후보 거부.
  - `uncertain=false`만으로 검토 불필요를 정하지 않는다.
- **미배정 OCR**: Region 초안에 넣지 않는다. 경계에 걸친 OCR은 "배정되지 않은 참고"로 따로 표시해 전달한다. 최종 본문에 미배정 목록과 Region 문구를 무조건 합치지 않는다.
- **중복 측정**: 중첩 Region 사이의 반복 줄은 `cross_region_duplicates`로 측정만 하고 삭제하지 않는다.
- **동시성**: 비교 실험은 동시 1이다. 새 입력 형식의 품질이 확인되기 전에는 1/2/4 비교를 하지 않는다.
