# 12. 입력 파일별 파싱 결과 계약(layout-result/2) — 라벨링 전 단계

지시: 2026-10-27 사용자 프롬프트(파일별 파싱 JSON 구조 확정 + 새 입력 실행). 이전 프롬프트의 HTML 표 신규 구현은 범위에서 뺐다. 기존 표 처리와 결과는 그대로 둔다.

**이번 작업에서 하지 않은 것**
- 상품 분리·상품 배정·템플릿 선택·라벨링 추론을 구현하지 않았다. 해당 필드와 '미실행' 상태만 둔다.
- 표의 새 추출 방식·HTML 표현·의미 설명·구조 복구를 구현하지 않았다. 기준 결과에 쓴 table-cells/1 경로와 실패 처리를 그대로 쓴다.

## 1. 기본 프로필(page-first-default/1)과 실행 흐름

기준 결과는 `work/pipeline/page-first-docs11-regression-2026-10-26`이다. `scripts/run_pipeline.py`의 기본값을 이 프로필로 바꿨다.

| 항목 | 값 |
|---|---|
| 판독 | `--read-mode page-first`(기본) + `--verify-mode reread`(기본, region-reread/1) |
| 표 | `--table-read cells`(기본, table-cells/1). 실패하면 page-first는 1차 page 응답 묶음을 유지하고 `table_structure_failed`로 표시한다. 재시도는 없다 |
| OCR 배정 | strict(core-containment/1, core_shrink 0) |
| VLM 입력 띠 | page-band/1(서버 축소 0.7배 미만 페이지만, VLM 입력에만 적용) |
| 순서 확인 | simple-column-order/1(재판독 조건과 품질 검토 표시) |
| 긴 이미지 | 기본으로 승인 경계(`fixtures/boundary-plans`, 원본 해시가 같은 페이지에만)와 자동 경계 정책(section-pack/2)을 쓴다. 정책 범위 밖이면 후보 경계로 실행하고 CANDIDATE_RUN·candidate_unvalidated로 표시한다(끄려면 `--no-run-candidate-boundaries`) |
| 미배정 출력 | unassigned-output/1(7절). 결과의 미배정은 어느 영역에도 면적 50% 이상 들어가지 않은 OCR 줄만 |
| 실행 옵션 | 기본 프로필은 `--manifest`와 `--out`만 지정한다(VLM 동시 2) |
| 이전 경로(비교용) | `--read-mode crop`, `--table-read legacy`, `--verify-mode patch` |

실행 흐름은 다음과 같다.

1. 입력(manifest 순서)
2. 페이지 렌더(PDF·HWP 200DPI, 이미지는 그대로)
3. OCR: 일반 페이지는 전체 1회, 긴 이미지는 경계 탐색 후 타일
4. 레이아웃 영역과 strict 배정/미배정
5. VLM 판독
   1. 페이지·타일 판독(page/2, 필요하면 띠)
   2. 조건에 걸린 영역만 원본 crop과 문맥으로 재판독
   3. 표 후보는 table-cells/1로 판독
6. 실행 단위 결과(layout-result/1, `<out>/result.json`)
7. 입력 파일별 결과(layout-result/2, `<out>/files/<file_id>/result.json`)와 목록·대조 화면

## 2. 출력 단위와 폴더

```
<out>/files/index.json              파일 목록(입력 순서, file_id → result.json, 페이지·영역·미배정 수, result_id, 상태 요약)
<out>/files/index.html              전체 대조 화면(파일·페이지 선택, 영역별 최종 문구·표·검토·상품/라벨 상태)
<out>/files/contract-check.json     계약 검사 결과
<out>/files/<file_id>/result.json   입력 파일 하나의 결과(여러 페이지 PDF/HWP 는 모든 페이지)
<out>/result.json                   실행 단위 결과(layout-result/1, 내부·재구성용)
<out>/trace/                        VLM 요청 메타·원시 응답·판독 근거(reads.json)·시간
<out>/stage1/                       OCR 원시 응답·Stage 1 기록·렌더 페이지
```

- 파일 하나에 문서 하나를 만든다. 여러 파일을 한 JSON으로만 합치거나, 여러 페이지 파일을 페이지별로 나누지 않는다.
- 폴더 이름은 `file_id`다. 원본 파일명이 같아도 덮어쓰지 않으며, 같은 file_id가 두 번 나오면 오류로 멈춘다.
- 기존 실행은 `scripts/export_file_results.py --run <실행> --out <새 폴더>`로 새 계약으로 내보낸다. 원래 실행 폴더는 읽기만 한다.

## 3. 최상위 필드

| 필드 | 의미 |
|---|---|
| `contract` | `"layout-result/2"` |
| `result_id` | 파일별 파싱 snapshot 식별자 `lr2-<20 hex>`. 아래 해시를 넣은 sha256이다. 저장 응답으로 재구성해도 같다.<br>• 원본 파일 SHA-256<br>• parser_profile<br>• 이 파일 페이지의 최종 OCR 원시 응답(호출 ID, 파일 해시)<br>• 이 파일 영역에 쓰인 VLM 응답(호출 ID, 내용 해시) |
| `submission_id` | 제출 묶음 ID(manifest). 파일·묶음을 상품 단위로 보지 않는다 |
| `source` | `file_id`, `file_name`(원본 파일명), `input_order`(묶음 안 입력 순서, 1부터), `file_type`(image/pdf/hwp/hwpx), `sha256`(원본 파일), `page_count` |
| `parser_profile` | `name`, `read_mode`, `verify_mode`, `table_mode`, `assignment_rule{name, core_shrink}`, `versions`(page_request, page_band, reading_order_check, routing_policy, boundary_policy, run_candidate_boundaries, layout_options, pdf_dpi, vlm_temperature) |
| `pages[]` | 파일의 모든 페이지를 원래 순서대로. 실패·후보·보류 페이지도 빼지 않는다 |
| `regions[]` | 원시 레이아웃 영역(ID·bbox 그대로) |
| `unassigned_texts[]` | 영역에 배정되지 않은 OCR 원문(`final: false`) |
| `products[]` | 상품 참조 목록(`product_id`, `name`(string\|null)). 이번 단계에서는 `[]` |
| `trace_ref` | 실행 폴더·reads.json·Stage 1 기록의 상대 경로(근거 추적용) |

최종 JSON에 넣지 않는 것: 프롬프트, 중간 후보(1차 판독·재판독 후보·1차 표 행), 원시 응답, 임계값·측정값, 평가 점수·정답, 정답 평가 이슈(`GOLD_*`). 이 값들은 trace(`reads.json`, `trace/vlm/*.json`)와 평가 폴더에 있다.

## 4. pages[]

| 필드 | 의미 |
|---|---|
| `page_id`, `page_no` | 페이지 ID와 파일 안 번호(1부터) |
| `width`, `height`, `coordinate_system` | 원본(렌더) 페이지 픽셀 크기, `page_px_xyxy` |
| `image` | 렌더 이미지 `sha256`과 result.json 기준 상대 `path` |
| `route` | `whole`(일반 페이지 전체 OCR 1회) / `long`(긴 이미지 타일) |
| `execution_status` | `READY`, `CANDIDATE_RUN`(미검증 후보 경계로 실행, 확정 결과 아님), `BOUNDARY_PENDING`(경계 없음, 미처리), `OCR_FAILED` 등 |
| `quality_status` | `not_verified`(실행 완료이며 내용 품질은 별도 평가), `candidate_unvalidated`, `not_processed` |
| `boundary` | 긴 이미지의 `plan_status`, `plan_version`, `cuts`, `tiles`, `validated`. 일반 페이지는 null |
| `issues[]` | 페이지 진단 `code`, `severity`, `message`. 예: 영역 밖 OCR, 경계 걸침, 영역 겹침, 후보 경계, 판독 실패, 표 판정 없음 |

영역 안 판독이 모두 끝났더라도 페이지의 모든 문구가 추출됐다는 뜻은 아니다. 레이아웃 영역 밖 문구는 `unassigned_texts`와 `issues`(TEXT_OUTSIDE_LAYOUT 등)로 남는다.

## 5. regions[]

| 필드 | 의미 |
|---|---|
| `region_id`, `page_id`, `bbox` | 원래 layout region_id와 원본 페이지 좌표 xyxy. 타일 좌표 이동 외에는 바꾸지 않는다(확장·병합·분할 없음) |
| `table_candidate` | 레이아웃이 표로 검출한 영역 |
| `text` | 선택된 최종 VLM 문구. OCR 원문·미배정 OCR을 섞지 않는다 |
| `text_status` | `read`(text 문자열) / `no_text`(text `""`, 글자 없음으로 판독) / `read_failed`(text null, 판독 실패) / `not_read`(text null, 판독 대상 아님·미실행) |
| `table` | 표 후보가 아니면 null. 표 후보면 기존 표 결과를 그대로 둔다(아래) |
| `review` | `required`(검토 표시), `quality_reasons`(품질 근거), `context_reasons`(맥락 근거). `required=false`는 사람 승인이나 정확도 보장이 아니다 |
| `candidate_boundary` | 미검증 후보 경계 타일의 영역 |
| `product_assignment` | 6절 |
| `labeling` | 6절 |

**table**(기존 코드 출력 그대로, 새 계약으로 재설계하지 않음)

- `status`: `table`, `not_table`, `unknown`(표 판정 없음. false로 바꾸지 않는다).
- `rows`: 문자열 행 배열. table이 아니면 null.
- table-cells/1 결과는 다음을 더 갖는다.
  - `structure`: `"table-cells/1"`.
  - `n_rows`, `n_cols`.
  - `cells[{row, col, rowspan, colspan, text}]`: 논리 셀. 병합 문구는 시작 칸에 한 번 쓰고, rows는 셀에서 변환한 보기다.
- 이전 경로 결과는 `structure`가 `"rows/legacy"`이고 cells가 없을 수 있다.

## 6. 상품 배정·라벨링 필드(영역 객체 안)

이번 결과의 초기값(모든 영역):

```json
"product_assignment": {"status": "not_run", "scope": null, "product_ids": []},
"labeling": {"status": "not_run", "items": []}
```

| 규칙 | 내용 |
|---|---|
| `status` | `not_run`(미실행) / `completed` / `partial`(일부만 확정) / `failed`. `not_run`의 빈 배열은 '상품·라벨 없음' 판정이 아니다 |
| `product_assignment.scope` | `specific`(특정 상품 전용) / `shared`(여러 상품 공통) / `unknown`(미확정) / 미실행이면 null |
| 복수 상품 | `product_ids` 배열로 한 영역이 여러 상품을 참조할 수 있다(`shared`) |
| 여러 파일·페이지·타일 | 같은 product_id를 다른 파일·페이지·타일 영역이 함께 참조할 수 있다. 같은 제출 묶음에서 같은 product_id는 같은 상품이다 |
| `products[]` | 문서 안 영역·라벨이 참조한 product_id를 모두 담는다(`product_id`, `name` string\|null). 묶음 안 여러 파일에서 같은 id의 이름이 다르면 오류 |
| `labeling.items[]` | `{product_id(string\|null), label_ids[]}`. product_id가 있으면 그 영역의 product_ids 안에 있어야 한다. 실제 구분값 사전이 없으므로 라벨을 임의로 만들지 않는다 |
| `not_run` 제약 | `scope=null`, `product_ids=[]`, `items=[]`(스키마로 강제) |
| `completed` 제약 | `specific`·`shared`이면 product_ids가 1개 이상이어야 한다. `completed`·`partial`은 scope가 필요하다 |
| 원문 보존 | 상품 소속을 맞추려고 bbox·text·ID를 바꾸지 않는다. 후속 상품 배정·라벨링은 이 필드만 채운 새 snapshot으로 남기고 원래 파싱 snapshot(result_id)은 보존한다 |

## 7. unassigned_texts[]

`ocr_id`, `page_id`, `bbox`(OCR 원래 bbox), `ocr_text`(OCR 원문), `reason`, `candidate_region_ids`, `empty_text`, `final: false`를 둔다.

- **대상(unassigned-output/1, 2026-10-27)**: Stage 1 엄격 배정에서 미배정인 OCR 줄 중 **어느 영역에도 OCR 상자 면적의 50% 이상 들어가지 않은 줄**만 둔다. 영역 밖 문구(`outside_layout`)와 영역에 거의 걸치지 않은 줄이다.
  - 면적 50% 이상이 한 영역 안인 줄은 그 영역 안 글자로 보고 넣지 않는다. VLM 지시('경계에 걸친 줄은 대부분이 상자 안이면 그 영역에 쓴다')와 재판독·품질 검토의 OCR 근거 기준과 같은 값이다. 그 영역 문구에 반영되지 않았으면 영역 품질 검토(OCR↔VLM 불일치·숫자 불일치)로 표시된다.
  - Stage 1 기록(`stage1/records`)과 VLM 입력은 바꾸지 않는다. 결과 출력의 분류만 바꾼다. 규칙 이름은 `parser_profile.versions.unassigned_output`에 남고 result_id 계산에 들어간다.
  - 확인(호출 0, `work/pipeline/tmp-inputs-2026-10-27-r1-unassigned50`): 영역 388개 값 변경 0, 미배정 531 → 127(`outside_layout` 116, 거의 걸치지 않은 `partial_overlap` 11).
- `reason`: `outside_layout`, `partial_overlap`(영역 안 비율 50% 미만), `ambiguous_*`.
- 페이지 `issues`의 `TEXT_PARTIAL_IN_REGION` 등은 Stage 1 배정 진단이며 미배정 목록과 수가 다를 수 있다.
- VLM 최종 문구가 아니며 영역 text에 합치지 않는다.

## 8. 검증 도구

- JSON Schema: `schemas/layout-result-2.schema.json`(Draft 2020-12). 텍스트·상태 일치, 표 null 규칙, not_run 값 제약을 포함한다.
- 참조 규칙: `layout_pipeline/contract_v2.py`(`check_document`, `check_submission`). 스키마로 표현하기 어려운 product·라벨 참조, ID 유일성, page 참조, 묶음 안 이름 일관성을 본다.
- mock: `fixtures/contract-mocks/submission-two-files.json`. 다음 사례를 담는다.
  - 복수 상품 공통 영역
  - 두 파일의 같은 상품
  - 특정 상품 전용 영역
  - 미확정
  - 상품별 라벨
  - 미실행

  mock 값은 실제 결과에 넣지 않는다.
- 테스트: `tests/test_file_result.py`. 다음을 본다.
  - 파일 분리·보존
  - 여러 페이지 파일이 문서 하나에 담기는지
  - 같은 파일명 분리와 file_id 덮어쓰기 방지
  - 입력 순서, 빈 문구와 실패의 구분
  - result_id 유지
  - 참조·상태 위반 8종 거부

## 9. 기존 결과 내보내기와 보존 검사(모델 호출 0)

`scripts/export_file_results.py`로 기존 실행을 새 폴더에 파일별로 내보냈다. 원래 실행은 수정하지 않았다. 분리 전후로 다음이 같은지 검사했다.
- 페이지·영역·미배정 OCR 수
- 원래 ID·bbox
- text·text_status
- 표 결과·상태
- 검토 사유(required·quality·context)
- 후보 경계 표시
- 상품·라벨 초기값

| 기존 실행 | 내보낸 곳 | 파일 | 페이지 | 영역 | 미배정 | 보존 검사 | 계약 |
|---|---|---:|---:|---:|---:|---|---|
| page-first-docs11-regression-2026-10-26(19페이지) | work/export/page-first-docs11-regression-v2 | 14 | 19 | 283 | 501 | 차이 0 | 14/14 |
| new-inputs-page-first-final-2026-10-26 | work/export/new-inputs-page-first-final-2026-10-26-v2 | 2 | 2 | 48 | 46 | 차이 0 | 2/2 |
| new-inputs-crop-final-2026-10-26 | work/export/new-inputs-crop-final-2026-10-26-v2 | 2 | 2 | 48 | 46 | 차이 0(판독 실패 1 그대로) | 2/2 |

- 여러 페이지 파일은 문서 하나로 내보냈다: f07 2쪽, f12 2쪽, h01(HWP) 4쪽.
- 같은 실행을 두 번 내보내면 result_id가 같다.
- 저장 응답으로 재구성(`run_pipeline.py --rebuild`)하면 파일별 문서와 result_id가 같다. 상대 경로만 다르다.

## 10. 새 입력 첫 실행(tmp 9개, 2026-10-27)

- 입력: `C:/Users/cccjj/cginside/농협프로젝트/nh실무심의사례(정답자료있는)/tmp`.
  - manifest `work/manifests/tmp-inputs-2026-10-27.json`(제출 묶음 1개, 폴더 파일명 순 t01~t09).
  - 고정 프로필 `work/manifests/tmp-inputs-2026-10-27.profile.json`(지시문·코드 해시 포함).
- t01·t02는 기존 f02·f03과 SHA-256이 같다(새 데이터 아님). 새 데이터는 7개 파일이다.
  - PDF 2쪽, JPEG 1장, PNG 긴 이미지 2장, PDF 8쪽, PDF 2쪽, PDF 1쪽.
  - HWP·HWPX는 제공되지 않아 미검증이다.
- 실행: `work/pipeline/tmp-inputs-2026-10-27-r1`(첫 실행 그대로 보존, 재실행 없음).
  - 18쪽 모두 실행했다. READY 16, CANDIDATE_RUN 2(긴 PNG 2장, 폭 720px가 자동 경계 검증 범위 밖).
  - 영역 388개(read 378, no_text 10, 판독 실패 0), 미배정 OCR 531개(글자 있음 523). 계약 9/9 통과.
- 호출과 시간:
  - OCR 40회(경계 탐색 14 + 최종 26), 경계 관계 판정 VLM 17회.
  - 판독 VLM 187회(페이지 27, 재판독 145, 표 셀 15). 실패 호출 7: 페이지 출력 퇴화 3, 재판독 2, 표 2(모두 한도 잘림).
  - Stage 1 116초, 판독 1,784초(동시 2), 합계 1,902초.
- 원본 대조(AI): `work/review/tmp-inputs-2026-10-27-r1.md`.
  - 레이아웃 영역이 놓친 문구: 표지 제목, 핵심 문구, 심의필, 연락처, 배지.
  - 표 셀 판독 계약 실패 9/15(대형 표). 실패한 표는 1차 행을 유지하고 표시한다.
  - 아이콘·작은 상자 재판독의 이웃 문구 유입(표시됨).
  - 로고·아이콘 오독(표시 없음).
- t01·t02 정답 대조: 영역 안 정답 줄은 모두 찾았다(179/179, 52/52). 기존 실행(f02·f03)과 같은 수준이다.
