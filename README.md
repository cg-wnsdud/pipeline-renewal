# layout-pipeline

금융 광고 입력(이미지·PDF·HWP)을 페이지별 영역과 최종 문구로 파싱하고, 광고 상품·템플릿·영역 소속·라벨까지 정해 파일별 JSON 과 확인 화면을 만드는 파이프라인.

## 파일 구조

```
layout_pipeline/            파이프라인 코드
  pages.py, hwp_bridge.py     1 입력 → 페이지(PDF 200dpi 렌더, HWP/HWPX 는 PDF 변환)
  routing.py, auto_boundary.py, boundary.py
                              긴 이미지 판정·자동 경계(타일 나누기)
  stage1.py, ocr_client.py, regions.py, assignment.py, diagnostics.py
                              2 OCR 호출·레이아웃 영역·OCR 글자 배정
  read_stage.py, page_read.py, vlm_tasks.py, table_cells.py, vlm_client.py, vlm_contract.py
                              3 VLM 영역 판독(페이지 판독·재판독·표 셀 구조)
  supplement.py               4 놓친 영역 보충
  final_output.py, file_result.py, contract_v2.py, final_viewer.py, viewer.py
                              파싱 결과(layout-result/2)·계약 검사·확인 화면
  semantic.py, classify.py    5 상품·템플릿·영역 소속 결정(observe)
  labeling.py                 6 영역별 라벨링
  file_result_v3.py, contract_v3.py, report_html.py, semantic_assets.py
                              7 최종 결과(layout-result/3)·계약 검사·입력 검토 화면·분류 자산
  config.py, geometry.py, jsonio.py, text_eval.py, gold_eval.py, report.py
                              공통 도구(설정·좌표·JSON·문구 비교·정답 대조·Stage 1 보고)
scripts/
  run_pipeline.py             전체 실행(입력 → 파싱 → 보충 → 의미 단계 → 결과·화면). --rebuild 는 저장 응답으로 재구성
  run_semantic.py             저장된 파싱 결과로 의미 단계만 실행·재구성
  make_pipeline_report.py     입력별 실행 여러 개를 입력 검토 화면 하나로 합침
  import_semantic_assets.py   분류·라벨 자산 확인(--check)·정책 해시 갱신
config/                     레이아웃 옵션, 긴 이미지 분기·자동 경계 정책
assets/semantic/            템플릿 카탈로그·라벨 스키마·분류 정책(manifest 로 해시 고정)
schemas/                    layout-result-2(파싱)·layout-result-3(최종) JSON 스키마
fixtures/                   승인된 긴 이미지 경계, 정답 대조용 페이지 정답, 계약 테스트 모의 입력
tests/                      단위 테스트(모델 호출 없음, `pytest`)
docs/                       설계·실험 기록
work/                       실행 산출물(git 제외)
.env.example                OCR·VLM 서버 등 환경 변수 견본(.env 로 복사)
```

## 파이프라인 흐름

```
입력(업로드 하나 = 묶음 하나)
 → 1 페이지 준비 → 2 OCR 영역·글자 검출 → 3 VLM 영역별 최종 문구 판독 → 4 놓친 영역 보충
 → 파싱 결과 저장(파일별 layout-result/2)
 → 5 상품·템플릿·영역 소속 결정 → 6 영역별 라벨링 → 7 최종 JSON(파일별 layout-result/3)·확인 화면
```

실행: `python scripts/run_pipeline.py --manifest <입력 정의 json> --out work/pipeline/<새 폴더>`

출력: `<out>/files/<file_id>/result.json`(파싱), `<out>/semantic/files/<file_id>/result.json`(최종), `<out>/semantic/report.html`(입력 검토 화면), `<out>/trace/`(판단 근거·원시 응답·시간). 최종 JSON 의 필드는 아래 "최종 산출물 JSON 구조"에 정리했다.

## 단계별 로직

1. **입력·페이지 준비**: 한 번의 업로드를 하나의 광고 묶음으로 처리한다. PDF 는 200dpi 이미지로, HWP/HWPX 는 기존 변환 경로(hwp_bridge)로 PDF 를 만든 뒤 같은 방식으로 페이지를 만든다.
2. **OCR 영역·글자 검출**: 일반 페이지는 전체를 한 번에 OCR 서버(PP-StructureV3)에 보낸다. 세로가 가로의 3배 이상인 긴 이미지는 승인된 경계 문서가 있으면 그 경계로, 없으면 자동 경계(영역 사이 빈 띠 중 제목–본문을 가르지 않는 곳, section-pack/2)로 타일을 나눠 보낸다. 자동 경계 정책의 검증 범위(폭 640~700px) 밖이면 후보 경계로 실행하고 `candidate_pages` 검토를 붙인다. 레이아웃 상자가 영역(`…-L000`)이 되고 OCR 글자는 영역에 배정하거나 미배정으로 남긴다.
3. **VLM 영역별 최종 문구 판독**: 페이지(긴 페이지는 띠·타일) 이미지에 영역 상자를 그려 보여 주고 영역마다 최종 문구를 받는다. OCR 은 참고이고 최종 문구는 VLM 판독 결과다. 누락·순서 어긋남·상자 밖 문구 등 조건에 걸린 영역만 원본 crop 으로 재판독하며, 재판독이 다른 영역 글자를 가져오면 채택하지 않는다. 표 후보는 문구와 행·열 셀 구조를 함께 받는다.
4. **놓친 영역 보충**: 판독 뒤 어느 영역에도 들지 않은 OCR 글자를 같은 줄·위아래로 붙은 것끼리 묶고(2행×2열 이상이면 표 후보), 그런 글자가 있는 페이지는 표 문턱을 0.3 으로 낮춘 OCR 을 한 번 더 받아 표 상자를 찾는다. 새 영역은 원본에서 다시 읽고, 기존 영역과 겹치거나 문구가 이웃 영역에 이미 있으면 버린다. 추가 영역은 `…-S000` ID 와 `supplement_region` 검토로 구분하고 근거는 `trace/supplement.json` 에 남긴다.
5. **상품·템플릿·영역 소속 결정(observe)**: 영역 상자 이미지와 전체 영역의 파싱 결과(문구·표)를 VLM 에 보내 상품이 몇 개인지, 상품별 템플릿, 모든 영역이 어느 상품에 속하는지(한 상품·여러 상품 공통·미확정)를 한 호출에서 함께 정한다. 응답의 상품 번호·영역 연결을 검사하고, 이름도 자기 영역도 없는 대상은 버리고(R1) 이름 없는 대상이 같은 템플릿 대상과 겹치면 합친다(R2). 큰 묶음(4쪽·80영역·16,000자·이미지 6장 초과)은 페이지별로 확인한 뒤 통합 호출로 파일·페이지에 이어지는 같은 상품을 잇는다. 요청에는 파일명·파일 ID 를 넣지 않는다.
6. **영역별 라벨링**: 영역에 연결된 상품의 템플릿 라벨 스키마에서 해당 구분값을 고른다. 여러 상품의 공통 영역은 상품별로 라벨링하고, 소속이 불명확한 영역은 미확정으로 남긴다. 라벨 근거 인용은 영역 문구에서 확인한다.
7. **최종 JSON·확인 화면**: 파일마다 `layout-result/3`(페이지, 영역 bbox·최종 문구·표 결과, 상품·템플릿, 영역의 상품 연결·라벨, 검토 상태)을 쓰고 스키마·계약 검사를 한다. 상세 판단 근거·프롬프트·원시 응답은 `trace/` 에 따로 두며, 저장 응답만으로 같은 결과를 다시 만들 수 있다.

VLM 호출은 `--vlm-concurrency`(기본 2) 상한 안에서 동시에 보낸다. 긴 이미지 경계 판정은 한 라운드의 후보를 함께 보내고, 판독은 모든 파일·페이지·타일·띠를 한 작업 풀에 넣어 1차 판독이 끝난 단위부터 그 단위의 재판독·표 셀을 같은 풀에 넣는다. 의미 단계는 모든 호출이 같은 상한을 함께 쓴다. OCR 은 직렬이다. 동시 2·3·4 비교는 `docs/17-parallel-calls-and-concurrency.md`.

## 최종 산출물 JSON 구조

입력 파일 하나가 JSON 문서 하나다. 여러 페이지 PDF·HWP 도 문서 하나이고, 같은 제출 묶음의 파일들은 `submission_id` 와 상품 정의(`products[]`)를 공유한다. 최종 산출물은 `layout-result/3`, 그 근거인 파싱 결과는 `layout-result/2` 이며 스키마는 `schemas/` 에 있다.

| 파일 | 내용 |
|---|---|
| `<out>/semantic/files/<file_id>/result.json` | **최종 산출물**(layout-result/3). 파싱 값에 상품·영역 소속·라벨을 더한 것. `run_semantic.py` 단독 실행이면 `<out>/files/<file_id>/result.json` |
| `<out>/files/<file_id>/result.json` | 파싱 결과(layout-result/2). 상품·라벨은 `not_run`. 최종 문서의 파싱 값(bbox·text·table·review)은 이것과 같고 계약 검사가 보존을 확인한다 |
| `files/index.json` | 파일 목록과 요약(쪽·영역 수, 상태 분포). 최종 쪽 목록은 상품과 `parse_review`·`semantic_review` 수도 담는다 |
| `files/contract-check.json` | 스키마·참조·보존·라벨 인용 위치 검사 결과 |
| `semantic/report.html`, `trace/` | 검토 화면, 근거(프롬프트·원시 응답·인용·시간). 최종 JSON 에는 넣지 않는다 |

### 최상위

```jsonc
{
  "contract": "layout-result/3",
  "result_id": "lr3-76fa9c1c5da976a80e16",   // 파싱 result_id + 의미 단계 버전·응답 해시. 저장 응답으로 재구성해도 같다
  "submission_id": "tp03",                   // 제출 묶음
  "source": {"file_id": "tp03", "file_name": "<원본 파일명>", "input_order": 1, "file_type": "pdf",   // image|pdf|hwp|hwpx
             "sha256": "30dd…", "page_count": 2},
  "parse": {"contract": "layout-result/2", "result_id": "lr2-aef0…", "profile": "page-first-default/1",
            "path": "../../../files/tp03/result.json"},                 // 근거 파싱 결과(result.json 기준 상대 경로)
  "semantic": {"profile": "semantic-observe/1", "classification_mode": "observe",
               "status": "completed",                                    // completed|partial|failed(묶음 단위)
               "versions": {"observe": "observe/7", "integrate": "integrate/4", "label": "label/3", "…": "…"}},
  "pages": [],  "products": [],  "regions": [],  "unassigned_texts": [],
  "trace_ref": {"parse_result": "…", "semantic_trace": "../../trace/semantic/tp03.json"}
}
```

### pages[]

| 필드 | 의미 |
|---|---|
| `page_id`, `page_no` | 페이지 ID, 파일 안 번호(1부터). 처리 못 한 페이지도 빠지지 않는다 |
| `width`, `height`, `coordinate_system` | 원본 렌더 이미지 픽셀 크기(PDF 200dpi), 좌표계 `page_px_xyxy` |
| `image` | 렌더 이미지 `sha256`, result.json 기준 상대 `path` |
| `route` | `whole`(일반 페이지, OCR 1회) · `long`(긴 이미지, 타일로 나눠 처리) |
| `execution_status` | `READY` · `CANDIDATE_RUN`(미검증 후보 경계로 실행, 확정 결과 아님) · `BOUNDARY_PENDING`(경계 없어 미처리) · `OCR_FAILED` 등 |
| `quality_status` | `not_verified`(실행 완료, 내용 품질은 별도) · `candidate_unvalidated` · `not_processed` |
| `boundary` | 일반 페이지는 `null`. 긴 이미지는 `plan_status`, `plan_version`, `cuts`, `tiles[{view_id, crop, aspect}]`, `validated` |
| `issues[]` | 페이지 진단 `{code, severity(info\|review\|error), message}`. 예: `TEXT_OUTSIDE_LAYOUT`, `REGION_OVERLAP`, `BOUNDARY_CANDIDATE_RUN`, `REGION_READ_FAILED` |

### products[]

묶음에서 찾은 광고 상품 목록이다. 같은 묶음의 모든 파일 문서에 같은 정의가 들어간다.

| 필드 | 의미 |
|---|---|
| `product_id` | `product_001`… (묶음 안 첫 등장 순) |
| `name` | 광고 영역 문구에 그대로 있는 상품명. 없으면 `null`(이름을 만들지 않는다) |
| `product_group` | `예금성` · `대출성` · `투자성` · `카드` · `보험` · `기타` · `null` |
| `status` | `classified` · `undetermined`(템플릿 미확정) · `unsupported`(지원 범위 밖). `classified` 가 아니면 `templates` 는 비고 라벨링하지 않는다 |
| `templates[]` | `{template_id, role, label_schema}`. `role` 은 `product` 또는 `component`(퇴직연금 운용상품 FUND·ETF·ELB, `label_schema` 는 `null`). `label_schema` 는 `assets/semantic/label-schemas/` 의 파일 이름 |
| `pension` | 퇴직연금이 아니면 `null`. 아니면 `{investment_products: included\|not_exposed\|undetermined, plans: [IRP\|DC\|DB], components: [FUND\|ETF\|ELB]}` |
| `files`, `pages` | 상품이 나오는 파일 ID, 페이지 ID(묶음의 다른 파일 페이지 포함) |
| `review` | `{required, codes}` 상품 단위 검토. 예: `candidate_pages`(미검증 후보 경계 페이지의 영역 포함) |

### regions[]

레이아웃이 검출한 영역 하나가 항목 하나다. 의미 단계는 영역을 나누거나 합치지 않고 bbox·text 도 바꾸지 않는다.

| 필드 | 의미 |
|---|---|
| `region_id` | `<page_id>-v0-L000` 일반 페이지 영역, `<page_id>-t1-L003` 긴 이미지 타일 영역(`t<n>`=n번째 타일). `-S000` 은 놓친 영역 보충으로 추가한 영역 |
| `page_id`, `bbox` | 소속 페이지, 원본 페이지 픽셀 `[x0, y0, x1, y1]`. 타일 안 영역도 타일이 아닌 페이지 좌표 |
| `table_candidate` | 레이아웃이 표로 검출한 영역 |
| `text` | VLM 이 판독한 최종 문구. OCR 원문이 아니다 |
| `text_status` | `read`(text 문자열) · `no_text`(`""`, 글자 없음으로 판독) · `read_failed`(`null`, 판독 실패) · `not_read`(`null`, 판독 대상 아님) |
| `table` | 표 후보가 아니면 `null`. 표 후보는 `{status: table\|not_table\|unknown, rows, structure, n_rows, n_cols, cells[{row, col, rowspan, colspan, text}]}`. `cells` 가 원본 셀 구조(병합 문구는 시작 칸에 한 번), `rows` 는 셀에서 만든 보기 |
| `review` | **파싱 검토** `{required, quality_reasons, context_reasons}`. 품질 근거는 판독 신뢰(예: `digit_mismatch`, `ocr_vlm_mismatch`, `reading_order_conflict`), 맥락 근거는 경계·보충·겹침(예: `ocr_crosses_region_boundary`, `candidate_boundary_unvalidated`, `supplement_region`) |
| `candidate_boundary` | 미검증 후보 경계 타일의 영역 |
| `product_assignment` | 영역이 속한 상품(아래) |
| `labeling` | 영역의 상품별 라벨(아래) |
| `semantic_review` | **의미 검토** `{required, codes}`. 소속·라벨 단계의 코드(예: `assignment_unknown`, `mixed_parts_missing`, `label_quote_rejected`). 파싱 검토와 따로 둔다 |

**product_assignment** `{status, scope, product_ids, basis}`

| status · scope | 뜻 | product_ids |
|---|---|---|
| `completed` · `specific` | 한 상품 전용 영역 | 1개 |
| `completed` · `shared` | 여러 상품에 공통(회사명·머리글·고지·심의번호 등) | 1개 이상 |
| `completed` · `mixed` | 한 영역 안에 상품별 내용이 섞임 | 2개 이상 |
| `partial` · `unknown` | 소속 미확정. 라벨링하지 않는다(`labeling.status=not_run`) | 빈 배열 |
| `failed` · `not_run` | 분류 호출 실패·미실행. `scope` 는 `null` | 빈 배열 |

`basis` 는 `model`(모델 배정), `single_product`(후보 상품이 하나라 호출 없이 배정), `null`(미실행·실패)이다.

**labeling** `{status, items[{product_id, label_ids}]}`: 배정된 상품마다 항목이 하나 있고 `label_ids` 는 그 상품 템플릿의 라벨 스키마에 있는 구분값 이름(예: `심의번호`, `가입기간`, `금리`)이다. 상품이 둘 이상인 영역은 상품별로 따로 라벨링한다. `completed` 에서 `label_ids` 가 빈 배열이면 "해당 라벨 없음"으로 판정한 것이고, `not_run`·`failed` 의 빈 `items` 와 다르다(사유는 `semantic_review.codes`). `partial` 은 일부 상품만 라벨이 정해진 경우다.

### unassigned_texts[]

어느 영역에도 면적 50% 이상 들어가지 않은 OCR 줄이다(영역 밖 문구 등). `{ocr_id, page_id, bbox, ocr_text, reason, candidate_region_ids, empty_text, final}` 이고 `final` 은 항상 `false` 다. 최종 문구가 아니고 영역 `text` 에 합치지 않는다. `reason` 은 `outside_layout` · `partial_overlap` · `ambiguous_*` 이다.

### 영역 예시(표 영역, 실제 결과에서 발췌)

```jsonc
{
  "region_id": "tp03_p02-v0-L006", "page_id": "tp03_p02", "bbox": [1083.3, 725.4, 1523.5, 798.9], "table_candidate": true,
  "text": "기간 12개월 적용이자율 2.45%", "text_status": "read",
  "table": {"status": "table", "rows": [["기간", "12개월"], ["적용이자율", "2.45%"]], "structure": "table-cells/1", "n_rows": 2, "n_cols": 2,
            "cells": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "기간"}, "…"]},
  "review": {"required": false, "quality_reasons": [], "context_reasons": []},
  "candidate_boundary": false,
  "product_assignment": {"status": "completed", "scope": "specific", "product_ids": ["product_002"], "basis": "model"},
  "labeling": {"status": "completed", "items": [{"product_id": "product_002", "label_ids": ["가입기간", "금리"]}]},
  "semantic_review": {"required": false, "codes": []}
}
```

### 읽을 때 주의

- `review.required=false`, `semantic_review.required=false` 는 사람이 승인했다는 뜻도 정확도 보장도 아니다. 검토 근거가 없다는 뜻이다. 근거는 영역별 `quality_reasons`·`context_reasons`·`codes` 에 있다.
- 계약 검사(`contract-check.json`)는 스키마·참조·파싱 값 보존·라벨 인용 위치만 확인한다. 상품 분류·템플릿·라벨 내용이 맞는지는 검사하지 않는다.
- 긴 이미지가 `CANDIDATE_RUN` 이면(자동 경계가 검증 범위 폭 640~700px 밖) 그 영역에는 `candidate_boundary_unvalidated` 가 붙고 상품에는 `candidate_pages` 가 붙는다. 확정 결과가 아니다.
- 프롬프트, 라벨·상품 근거 인용, 특징 ID, 원시 응답, 중간 판독 후보, 임계값, 시간은 최종 JSON 에 없고 `trace/` 에 있다.
- 검토 코드 전체는 `docs/12-file-result-contract.md` §4~5(파싱), `docs/14-semantic-labeling.md` §3(의미)에 있다. layout-result/2 는 `parser_profile` 을 갖고 `products[]` 가 `{product_id, name}` 참조뿐이며 `semantic`·`semantic_review`·`basis` 가 없다.
