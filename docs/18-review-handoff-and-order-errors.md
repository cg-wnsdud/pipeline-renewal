# 18. 심의 단계 입력(review-input/1)·문구 순서 오류 원인(2026-10-28)

## 1. 심의 단계 입력

최종 결과(layout-result/3, 파일별)는 실행·버전·경계·진단·근거까지 담아 심의 단계에 바로 넘기기 어렵다. 묶음(submission) 하나마다 심의에 필요한
값만 추린 JSON 을 `<out>/semantic/handoff/<submission_id>.json`(`layout_pipeline/handoff.py`)으로 더 만든다. 구조는 README "심의 단계 입력".

- nh-ad-compliance 는 한 광고의 여러 파일을 한 번에 올리고 파서를 묶음 단위로 한 번 부른다(`parse_group`). 그래서 묶음 하나 = JSON 하나다.
- 값은 layout-result/3 그대로 옮긴다. `handoff/check.json` 이 영역 집합·bbox·text 보존과 상품·라벨 참조를 검사한다.
- 기존 parser-pipeline 출력(P3 `nh-ad-region-review-input-v11` + P1 `nh-ad-parse-evidence-v6`)과 다른 점(사용자 결정: 우리 구조 기준):

| 항목 | 기존 P3 | review-input/1 |
|---|---|---|
| 영역 ID | 페이지별 `pN_rNNN` 로 다시 매김 | layout-result/3 의 region_id 그대로 |
| 표 | `selected_text` 를 GFM 마크다운으로 바꾸고 `table.cells`(row_span·col_span·is_header) | `text` 는 판독 문구 그대로, `table` 은 셀 구조(n_rows·n_cols·cells[rowspan·colspan]). 머리글 여부 없음 |
| 상품 소속 | `product_id` 하나(`product_N`·`page_common`·`unknown`) | `scope` + `product_ids`(여러 상품 공통·혼합 영역 표현) |
| 라벨 | 영역당 목록 하나 | 상품별 `label_ids` |
| 템플릿 | 문서 수준 `document.template`(사용자 지정이면 `source=user_provided`) | 상품별 `templates[].labels` |
| 묶음 페이지 | `page_no` 를 묶음 전체로 다시 매기고 `source_file`·`source_page_no` | `page_id`·`file_id`·파일 안 `page_no` |
| 짝 근거 파일 | P1(OCR 줄·원래 영역 ID, 판정 엔진이 읽음) | 만들지 않음 |

parser-pipeline 을 이 파이프라인으로 바꿀 때 심의 쪽(worker `rule_judge.py`·`nh_parser.py` 의 계약 버전 검사, rule-engine `inputs.py`·`chunking.py`·
`source_binding.py`·`reading_quality.py`, backend `operational*.py`)이 위 차이를 받도록 맞춰야 한다. 사용자가 등록 때 고른 템플릿을 의미 단계에 넘기는 입력도
아직 없다.

## 2. 문구 순서 오류 원인(e2e-2026-10-28)

공통 원인: OCR 후보를 VLM 에 **문자열 순으로 정렬해** 준다(`vlm_tasks.candidate_context`, crop/2·3 과 page/2 가 같이 씀). OCR 배열 순서를 읽기
순서로 베끼지 않게 하려는 설계(docs/04)인데, 모델이 정렬된 순서를 따라 쓰는 경우가 있다.

### tp02 `t2-S004` 문장 순서가 섞임

1. 레이아웃 모델이 이 블록(이자지급 방법 본문·※ 각주)을 상자로 내지 않아 OCR 줄 6개가 모두 미배정 → 놓친 영역 보충이 OCR 묶음으로 S004 를 만들었다.
2. 보충 영역은 crop 묶음 판독(crop/3, 7영역 한 요청, `vlm-0009`)으로 읽는다. 넘긴 후보 순서가 `※예금의…`·`날부터는…`·`매년 3월…`·`있습니다.`·
   `직전 영업일…`·`추가 입출금…`(가나다순)이고, 출력은 이 순서와 거의 같다(`있습니다.` 만 끝으로). 원본 순서는 `매년…`→`직전…`→`※예금의…`→`날부터…`
   →`추가…`→`있습니다.` 다.
3. 순서 검사(simple-column-order/1)가 역전 1건을 찾아 `reading_order_conflict` 를 붙였지만, **보충 영역에는 재판독 단계가 없어** 그대로 남았다.

### tp06 `L002` "(직전 분기 수수료무료)" 가 위아래 두 번

1. OCR 에는 이 문구가 표 아래에 한 번만 있다. 후보 정렬에서 '(' 가 한글보다 앞이라 후보 목록 **맨 앞**에 왔고, 1차 페이지 판독(page/2, `vlm-0001`)이
   영역 문구 맨 앞에 한 번 쓰고 원래 위치(맨 끝)에도 썼다.
2. 검사가 반복(`repeated_text_in_page_read`)과 표 행 불완전을 잡아 표 셀 재판독(table-cells/1, `vlm-0003`)을 보냈고, 재판독 문구에는 한 번만 있었다.
3. 재판독 셀 구조가 계약 검사에 실패했다(`n_rows=8` 인데 9번째 행 셀 4개, (4,2)~(4,4) 빈 칸). 표 판독은 "부분 응답을 섞지 않는다" 규칙이라 재판독 전체를
   버리고 1차 페이지 판독(중복 포함)을 남겼다. 결과에 `repeated_text`·`table_structure_failed` 검토 표시가 있다.

### 고칠 방향(아직 바꾸지 않음, 판독 결과가 바뀌므로 측정 후 결정)

- 후보 표현: 문자열 정렬 대신 위치 순(위→아래) 또는 순서 정보를 주지 않는 다른 표현을 비교한다.
- 보충 영역도 본 판독과 같은 영역 확인(재판독) 조건을 적용한다.
- 표 셀 재판독이 구조만 실패하고 문구 검사는 통과하면 문구는 재판독 쪽을 쓰고 표 구조만 없음으로 두는 선택지를 비교한다.
