# layout-pipeline

금융 광고 입력(이미지·PDF·HWP)을 페이지별 영역과 최종 문구로 파싱하고, 광고 상품·템플릿·영역 소속·라벨까지 정해 파일별 JSON 과 확인 화면을 만드는 파이프라인.

## 파일 구조

```
layout_pipeline/            파이프라인 코드
  pages.py, hwp_bridge.py     1 입력 → 페이지(PDF 200dpi 렌더, HWP/HWPX 는 PDF 변환 + DOM 행)
  hwp_legacy/, hwp_structure.py
                              HWP 기존 parser-pipeline 경로(코드 그대로 복사)·구조 Region(docs/20)
  routing.py, auto_boundary.py, boundary.py
                              긴 이미지 판정·자동 경계(타일 나누기)
  stage1.py, ocr_client.py, regions.py, assignment.py, diagnostics.py
                              2 OCR 호출·레이아웃 영역·OCR 글자 배정
  read_stage.py, page_read.py, vlm_tasks.py, table_cells.py, vlm_client.py, vlm_contract.py
                              3 VLM 영역 판독(페이지 판독·재판독·표 셀 구조)
  supplement.py               4 놓친 영역 보충
  final_output.py, file_result.py, contract_v2.py, final_viewer.py, viewer.py
                              파싱 결과(layout-result/2)·계약 검사·확인 화면
  semantic.py, classify.py    5 상품·템플릿·영역 소속 결정(observe, 등록 템플릿 요청 반영)
  deposit_marks.py            예금자보호 보호·비보호 표시(로고) 확인(docs/21)
  labeling.py                 6 영역별 라벨링
  file_result_v3.py, contract_v3.py, report_html.py, semantic_assets.py
                              7 최종 결과(layout-result/3)·계약 검사·입력 검토 화면·분류 자산
  handoff.py                  심의 단계 입력(review-input/2)
  service.py, preview.py      파서 서비스 실행(요청 → 실행 폴더 + service.json)·원본 미리보기(파싱과 같은 렌더, docs/21)
  config.py, geometry.py, jsonio.py, text_eval.py, gold_eval.py, report.py
                              공통 도구(설정·좌표·JSON·문구 비교·정답 대조·Stage 1 보고)
scripts/
  run_pipeline.py             전체 실행(입력 → 파싱 → 보충 → 의미 단계 → 결과·화면). --rebuild 는 저장 응답으로 재구성
  run_semantic.py             저장된 파싱 결과로 의미 단계만 실행·재구성
  run_service.py              파서 서비스 실행(요청 JSON 하나 → 실행 폴더 + service.json, 종료 코드로 상태, docs/21)
  build_handoff.py            저장된 최종 결과로 심의 단계 입력만 다시 만들기
  make_pipeline_report.py     입력별 실행 여러 개를 입력 검토 화면 하나로 합침
  import_semantic_assets.py   분류·라벨 자산 확인(--check)·정책 해시 갱신
config/                     레이아웃 옵션, 긴 이미지 분기·자동 경계 정책
assets/semantic/            템플릿 카탈로그·라벨 스키마·분류 정책(manifest 로 해시 고정)
schemas/                    layout-result-2(파싱)·layout-result-3(최종)·review-input-2(심의 입력) JSON 스키마
fixtures/                   승인된 긴 이미지 경계, 정답 대조용 페이지 정답, 계약 테스트 모의 입력
tests/                      단위 테스트(모델 호출 없음, `pytest`)
docs/                       설계·실험 기록
work/                       실행 산출물(git 제외)
.env.example                OCR·VLM 서버 등 환경 변수 견본(.env 로 복사)
pyproject.toml, uv.lock     의존성(Python 3.11 이상, pypdfium2 고정). HWP 변환용 document-processor 는 별도 설치
```

## 파이프라인 흐름

```
입력(업로드 하나 = 묶음 하나)
 → 1 페이지 준비 → 2 OCR 영역·글자 검출 → 3 VLM 영역별 최종 문구 판독 → 4 놓친 영역 보충
 → 파싱 결과 저장(파일별 layout-result/2)
 → 5 상품·템플릿·영역 소속 결정 → 6 영역별 라벨링 → 7 최종 JSON(파일별 layout-result/3)·확인 화면
```

실행: `python scripts/run_pipeline.py --manifest <입력 정의 json> --out work/pipeline/<새 폴더>`

manifest 묶음에 `"template_request": {"template_ids": ["예금성상품-적립식"]}` 를 두면 등록 템플릿 요청이다(없음: 자동 분류, 1개: 모든 상품에
그 템플릿, 여러 개: 상품마다 그 안에서만, docs/21). 파서 서비스(nh-ad-compliance)는 `scripts/run_service.py --request <요청> --out <폴더>` 로 실행한다.

출력: `<out>/files/<file_id>/result.json`(파싱), `<out>/semantic/files/<file_id>/result.json`(최종), `<out>/semantic/report.html`(입력 검토 화면), `<out>/trace/`(판단 근거·원시 응답·시간). 최종 JSON 의 필드는 아래 "최종 산출물 JSON 구조"에 정리했다.

## 단계별 로직

1. **입력·페이지 준비**: 한 번의 업로드를 하나의 광고 묶음으로 처리한다. PDF 는 200dpi 이미지로, HWP/HWPX 는 기존 parser-pipeline 변환 코드(hwp_legacy, 사내 document-processor 가 있는 가상환경에서 별도 프로세스)로 PDF 와 DOM 행(문단·표 행의 원문과 화면 좌표)을 만든 뒤 같은 방식으로 페이지를 만든다.
2. **OCR 영역·글자 검출**: 일반 페이지는 전체를 한 번에 OCR 서버(PP-StructureV3)에 보낸다. 세로가 가로의 3배 이상인 긴 이미지는 승인된 경계 문서가 있으면 그 경계로, 없으면 자동 경계(영역 사이 빈 띠 중 제목–본문을 가르지 않는 곳, section-pack/2)로 타일을 나눠 보낸다. 자동 경계 정책의 검증 범위(폭 640~700px) 밖이면 후보 경계로 실행하고 `candidate_pages` 검토를 붙인다. 레이아웃 상자가 영역(`…-L000`)이 되고 OCR 글자는 영역에 배정하거나 미배정으로 남긴다. HWP 페이지는 기존 parser-pipeline hybrid 경로대로 DOM 행이 구조 영역(`…-H000`, 문구 = HWP 원문)이 되고, 레이아웃 상자는 구조 영역에 없는 것(그림 속 글자 등)만 남는다(docs/20).
3. **VLM 영역별 최종 문구 판독**: 페이지(긴 페이지는 띠·타일) 이미지에 영역 상자를 그려 보여 주고 영역마다 최종 문구를 받는다. OCR 은 참고이고 최종 문구는 VLM 판독 결과다. 누락·순서 어긋남·상자 밖 문구 등 조건에 걸린 영역만 원본 crop 으로 재판독하며(region-reread/2), 재판독이 다른 영역 글자를 가져오면 채택하지 않는다. 표 후보는 문구와 행·열 셀 구조를 함께 받는다(아래 "표 추출 방식"). 판독 검사의 OCR↔VLM 숫자 비교는 천 단위 쉼표(3,000)만 붙이고 그 밖의 쉼표·점은 구분자로 본다. 페이지 판독 출력 한도는 판독 단위 OCR 글자 수에 맞추고(6,000~12,000), 한도에서 잘린 응답은 끝까지 쓴 영역만 쓴다. HWP 구조 영역은 판독하지 않는다.
4. **놓친 영역 보충**: 판독 뒤 어느 영역에도 들지 않은 OCR 글자를 같은 줄·위아래로 붙은 것끼리 묶고(2행×2열 이상이면 표 후보), 그런 글자가 있는 페이지는 표 문턱을 0.3 으로 낮춘 OCR 을 한 번 더 받아 표 상자를 찾는다. 새 영역은 원본에서 다시 읽고, 기존 영역과 겹치거나 문구가 이웃 영역에 이미 있으면 버린다. 추가 영역은 `…-S000` ID 와 `supplement_region` 검토로 구분하고 근거는 `trace/supplement.json` 에 남긴다.
5. **상품·템플릿·영역 소속 결정(observe)**: 영역 상자 이미지와 전체 영역의 파싱 결과(문구·표)를 VLM 에 보내 상품이 몇 개인지, 상품별 템플릿, 모든 영역이 어느 상품에 속하는지(한 상품·여러 상품 공통·미확정)를 한 호출에서 함께 정한다. 응답의 상품 번호·영역 연결을 검사하고, 이름도 자기 영역도 없는 대상은 버리고(R1) 이름 없는 대상이 같은 템플릿 대상과 겹치면 합치며(R2), 상품명·유형 근거 없이 영역 하나의 문구만 근거인 이름 없는 대상은 유일한 이름 있는 대상에 합친다(R3). 큰 묶음(4쪽·80영역·16,000자·이미지 6장 초과)은 페이지별로 확인한 뒤 통합 호출로 파일·페이지에 이어지는 같은 상품을 잇는다. 그 뒤 이름이 같은(공백·표기 통일) 상품은 하나로 합치고, 여러 입력 묶음은 첫 입력에서 정한 템플릿을 유지하며 뒤 입력의 이름 없는 대상은 첫 입력 상품에 잇는다(first-input/1, docs/20). 요청에는 파일명·파일 ID 를 넣지 않는다.
6. **영역별 라벨링**: 영역에 연결된 상품의 템플릿 라벨 스키마에서 해당 구분값을 고른다. 여러 상품의 공통 영역은 상품별로 라벨링하고, 소속이 불명확한 영역은 미확정으로 남긴다. 라벨 근거 인용은 영역 문구에서 확인한다.
7. **최종 JSON·확인 화면**: 파일마다 `layout-result/3`(페이지, 영역 bbox·최종 문구·표 결과, 상품·템플릿, 영역의 상품 연결·라벨, 검토 상태)을 쓰고 스키마·계약 검사를 한다. 상세 판단 근거·프롬프트·원시 응답은 `trace/` 에 따로 두며, 저장 응답만으로 같은 결과를 다시 만들 수 있다.

VLM 호출은 `--vlm-concurrency`(기본 2) 상한 안에서 동시에 보낸다. 긴 이미지 경계 판정은 한 라운드의 후보를 함께 보내고, 판독은 모든 파일·페이지·타일·띠를 한 작업 풀에 넣어 1차 판독이 끝난 단위부터 그 단위의 재판독·표 셀을 같은 풀에 넣는다. 의미 단계는 모든 호출이 같은 상한을 함께 쓴다. OCR 은 직렬이다. 동시 2·3·4 비교는 `docs/17-parallel-calls-and-concurrency.md`.

## 최종 산출물 JSON 구조

입력 파일 하나가 JSON 문서 하나다. 여러 페이지 PDF·HWP 도 문서 하나이고, 같은 제출 묶음의 파일들은 `submission_id` 와 상품 정의(`products[]`)를 공유한다. 최종 산출물은 `layout-result/3`, 그 근거인 파싱 결과는 `layout-result/2` 이며 스키마는 `schemas/` 에 있다. 심의 단계에는 묶음 하나에 한 파일인 `review-input/2` 를 넘긴다(아래 "심의 단계 입력").

| 파일 | 내용 |
|---|---|
| `<out>/semantic/files/<file_id>/result.json` | **최종 산출물**(layout-result/3). 파싱 값에 상품·영역 소속·라벨을 더한 것. `run_semantic.py` 단독 실행이면 `<out>/files/<file_id>/result.json` |
| `<out>/files/<file_id>/result.json` | 파싱 결과(layout-result/2). 상품·라벨은 `not_run`. 최종 문서의 파싱 값(bbox·text·table·review)은 이것과 같고 계약 검사가 보존을 확인한다 |
| `<out>/semantic/handoff/<submission_id>.json` | **심의 단계 입력**(review-input/2). `handoff/check.json` 은 스키마·영역 보존·참조 검사. 저장 결과로 다시 만들기: `scripts/build_handoff.py --semantic <의미 단계 폴더>` |
| `<out>/service.json` | 서비스 실행(`run_service.py`)일 때만. 상태·실패 코드·심의 입력 경로(아래 "서비스 실행 결과") |
| `files/index.json` | 파일 목록과 요약(쪽·영역 수, 상태 분포). 최종 쪽 목록은 상품과 `parse_review`·`semantic_review` 수도 담는다 |
| `files/contract-check.json` | 스키마·참조·보존·라벨 인용 위치 검사 결과 |
| `stage1/pages/*.png` | 페이지 렌더 이미지(좌표 기준). HWP 는 `stage1/pages/hwp/` 에 변환 PDF·검토 HTML |
| `semantic/report.html`, `viewer/`, `trace/` | 검토 화면, 근거(프롬프트·원시 응답·인용·시간). 최종 JSON 에는 넣지 않는다 |

### 최상위 필드

| 필드 | 값 |
|---|---|
| `contract` | `"layout-result/3"` 고정 |
| `result_id` | `lr3-<20 hex>`. 파싱 result_id + 의미 단계 프로필·자산·지시 버전 + 그 묶음 응답 해시. 저장 응답으로 재구성해도 같다 |
| `submission_id` | 제출 묶음 ID(서비스 요청의 `submission_id`) |
| `source` | `file_id`(요청의 파일 ID, 영역·페이지 ID 앞부분), `file_name`, `input_order`(묶음 안 입력 순서, 1부터), `file_type`(`image`·`pdf`·`hwp`·`hwpx`), `sha256`(원본 파일), `page_count` |
| `parse` | 근거 파싱 결과 `{contract: "layout-result/2", result_id: "lr2-…", profile: "page-first-default/1", path}`. `path` 는 이 result.json 기준 상대 경로 |
| `semantic.profile`, `semantic.classification_mode` | `semantic-observe/1`, `observe`(고정 경로) · `legacy`(이전 실행 재구성용) |
| `semantic.status` | 묶음 단위 `completed` · `partial`(일부 영역·상품만 정해짐) · `failed`(분류 호출 실패) |
| `semantic.versions` | 지시·규칙·자산 버전(`observe`, `integrate`, `label`, `target_rules`, `decisive`, `first_input`, `template_request`, `deposit_marks`, `policy`, `assets`, `assets_digest`). 같은 값이면 같은 규칙으로 만든 결과다 |
| `semantic.template_request` | 등록 템플릿 요청 `{template_ids, mode}`. `mode`: `auto`(요청 없음, 자동 분류) · `fixed`(템플릿 하나를 모든 상품에) · `restricted`(여러 개, 상품마다 그 안에서만) |
| `pages[]`, `products[]`, `regions[]`, `unassigned_texts[]` | 아래 각 절 |
| `deposit_insurance_marks` | 예금보험공사 보호·비보호 표시(로고)가 광고에 보이는지(묶음 단위, 같은 묶음의 모든 파일에 같은 값). 아래 표 |
| `trace_ref` | `parse_result`(파싱 결과 경로), `semantic_trace`(묶음 판단 근거 `trace/semantic/<submission_id>.json`) |

**deposit_insurance_marks** `{version: "deposit-marks/1", protected: {...}, non_protected: {...}}`. 문구 후보(`예금보험공사`·`보호금융상품`·`비보호` 가 있는 영역, 짧은 것부터 12개)의 영역 이미지를 VLM 에 보여 표시가 보이는지 확인한다. 표시가 있어야 하는지는 판단하지 않는다(심의가 판단).

| status | code | region_ids |
|---|---|---|
| `PRESENT` | `null` | 표시가 보인 영역(1개 이상) |
| `ABSENT` | `null` | 빈 배열. 후보가 없거나 후보 영역에 표시가 없음 |
| `UNKNOWN` | `CHECK_FAILED`(확인 호출 실패) · `MARK_UNCLEAR`(흐려서 판단 불가) · `TOO_MANY_CANDIDATES`(후보 12개 초과분이 남음) · `PAGES_INCOMPLETE`(처리 못 한 쪽이 있음) · `DISABLED`(확인 끔) · `NOT_CHECKED`(이 기능 이전 결과) | 빈 배열 |

### pages[]

| 필드 | 값 |
|---|---|
| `page_id`, `page_no` | `<file_id>_p01`…, 파일 안 번호(1부터). 처리 못 한 페이지도 빠지지 않는다 |
| `width`, `height`, `coordinate_system` | 렌더 이미지 픽셀 크기(PDF·HWP 200dpi, 이미지는 원본 크기), 좌표계 `page_px_xyxy` |
| `image` | 렌더 이미지 `sha256`, result.json 기준 상대 `path` |
| `route` | `whole`(일반 페이지, OCR 1회) · `long`(세로가 가로의 3배 이상, 타일로 나눠 처리) |
| `execution_status` | `READY`(처리 완료) · `CANDIDATE_RUN`(미검증 후보 경계로 실행, 확정 결과 아님) · `BOUNDARY_PENDING`(경계 없어 미처리) · `OCR_FAILED`(OCR 호출 실패) |
| `quality_status` | `not_verified`(실행 완료, 내용 품질은 별도) · `candidate_unvalidated` · `not_processed` |
| `boundary` | 일반 페이지는 `null`. 긴 이미지는 `{plan_status, plan_version, cuts, tiles[{view_id, crop, aspect}], validated}` |
| `issues[]` | 페이지 진단 `{code, severity, message}`. `severity`: `info` · `review` · `error`. 코드는 아래 표 |

| issues.code | 뜻 |
|---|---|
| `TEXT_OUTSIDE_LAYOUT` / `TEXT_PARTIAL_IN_REGION` / `TEXT_AMBIGUOUS_REGIONS` / `TEXT_MARGIN_ONLY` | OCR 글자가 어느 영역에도 없음 / 영역에 일부만 걸침 / 여러 영역에 걸침 / 영역 가장자리에만 걸침 |
| `OCR_MULTIPLE_REGIONS_ASSIGNED` / `OCR_WEAK_OVERLAP_ASSIGNED` | OCR 줄 하나를 여러 영역에 배정 / 약하게 겹친 영역에 배정 |
| `REGION_OVERLAP` / `REGION_COVERS_LARGE_AREA` / `TILE_EDGE_REGION` | 영역끼리 겹침 / 영역 하나가 페이지 대부분을 덮음 / 타일 경계에 걸친 영역 |
| `NO_LAYOUT_REGIONS` / `OCR_CALL_FAILED` / `TEXT_DET_DOWNSCALED` | 레이아웃 영역 없음 / OCR 실패 / OCR 서버가 글자 검출 이미지를 줄임 |
| `REGION_READ_FAILED` / `TABLE_CHECK_UNKNOWN` / `TABLE_CONTAINER_READ_AS_TEXT` | 판독 실패 영역 있음 / 표 판정 없는 표 후보 / 문서 틀 표를 글 영역으로 판독 |
| `BOUNDARY_CANDIDATE_RUN` / `HWP_STRUCTURE_REGIONS` | 미검증 후보 경계로 실행 / HWP 구조 영역(원문)을 씀 |

### products[]

묶음에서 찾은 광고 상품 목록이다. 같은 묶음의 모든 파일 문서에 같은 정의가 들어간다.

| 필드 | 값 |
|---|---|
| `product_id` | `product_001`… (묶음 안 첫 등장 순) |
| `name` | 광고 영역 문구에 그대로 있는 상품명. 없으면 `null`(이름을 만들지 않는다) |
| `product_group` | `예금성` · `대출성` · `투자성` · `카드` · `보험` · `기타` · `null` |
| `status` | `classified` · `undetermined`(템플릿 미확정) · `unsupported`(지원 범위 밖). `classified` 가 아니면 `templates` 는 비고 라벨링하지 않는다 |
| `templates[]` | `{template_id, role, label_schema}`. `template_id` 는 판정 카탈로그 이름(예: `예금성상품-입출식`). `role` 은 `product` 또는 `component`(퇴직연금 운용상품 FUND·ETF·ELB, `label_schema` 는 `null`). `label_schema` 는 `assets/semantic/label-schemas/` 의 파일 이름 |
| `pension` | 퇴직연금이 아니면 `null`. 아니면 `{investment_products: included\|not_exposed\|undetermined, plans: [IRP\|DC\|DB], components: [FUND\|ETF\|ELB]}` |
| `files`, `pages` | 상품이 나오는 파일 ID, 페이지 ID(묶음의 다른 파일 페이지 포함) |
| `review` | `{required, codes}` 상품 단위 검토. 코드는 아래 표 |

분류하는 템플릿은 12개다(`assets/semantic/classification-policy.json`, 서비스 `GET /v1/templates`). 그 밖의 템플릿을 요청하면 OCR·모델 호출 전에 입력 오류로 멈춘다.

| 상품군 | template_id |
|---|---|
| 대출성 | `대출성상품-상품명 노출` · `대출성상품-상품명 미노출` · `대출성상품-대출모집인(잔금대출)` |
| 예금성 | `예금성상품-입출식` · `예금성상품-거치식` · `예금성상품-적립식` · `예금성상품-지수연동예금` · `예금성상품-상품명 미노출` |
| 투자성 | `투자성상품-개인종합자산관리계좌(ISA) 일반` · `투자성상품-개인종합자산관리계좌(ISA) 신탁형` · `투자성상품-개인종합자산관리계좌(ISA) 일임형` · `투자성상품-퇴직연금 일반`(운용상품 펀드·ETF·ELB 는 `role: component`) |

| products[].review.codes | 뜻 |
|---|---|
| `template_evidence_missing` / `template_conflict` / `multi_template_product` / `template_group_conflict` | 템플릿 특징 인용 없음 / 함께 둘 수 없는 템플릿 / 예금성 템플릿 둘(보통 별도 상품) / 상품군 충돌 |
| `template_from_request` | 등록 템플릿 하나(`fixed`)를 모든 상품에 둬서 모델 판단과 다름 |
| `decisive_signal_missing` | 결정 신호(예: 대출모집인)가 없는데 그 템플릿이 남음(`restricted` 에서 대체 템플릿이 등록 목록 밖) |
| `unnamed_template_with_name` / `named_template_without_name` | 상품명 미노출 템플릿인데 이름이 있음 / 상품명 노출 템플릿인데 이름이 없음 |
| `broker_with_named_loan` / `broker_signal_unused` | 대출모집인 + 상품명 노출 / 모집인 결정 신호가 문구에 있는데 모집인 템플릿 없음 |
| `isa_types_split` | 같은 파일에 ISA 일반과 신탁형·일임형이 함께 |
| `name_not_in_text` / `member_names_differ` | 모델 이름이 영역 문구에 없음(이름 null) / 묶은 대상들의 이름이 다름 |
| `template_undetermined` / `type_unsupported` | 템플릿 미확정 / 지원 범위 밖 유형 |
| `integration_failed` / `integration_missing_target` | 통합 요청 실패 / 통합 응답에 빠진 대상 |
| `candidate_pages` | 미검증 후보 경계(CANDIDATE_RUN) 페이지의 영역을 포함 |
| `pension_exposure_undetermined`, `pension_pages_incomplete`, `pension_unassigned_ocr_terms`, `pension_other_investment_only` | 퇴직연금 운용상품 노출 확정 불가 / 처리 못 한 페이지 / 영역 밖 OCR 에 운용상품 용어 / 국채·ELS·RP 만 소개 |

### regions[]

레이아웃이 검출한 영역 하나가 항목 하나다. 의미 단계는 영역을 나누거나 합치지 않고 bbox·text 도 바꾸지 않는다.

| 필드 | 값 |
|---|---|
| `region_id` | `<page_id>-v0-L000` 일반 페이지 영역, `<page_id>-t1-L003` 긴 이미지 타일 영역(`t<n>`=n번째 타일), `<page_id>-v0-S000` 놓친 영역 보충으로 추가한 영역, `<page_id>-h0-H000` HWP 구조 영역(문구 = HWP 원문) |
| `page_id`, `bbox` | 소속 페이지, 페이지 이미지 픽셀 `[x0, y0, x1, y1]`(소수). 타일 안 영역도 페이지 좌표 |
| `table_candidate` | 레이아웃이 표로 검출한 영역(또는 보충 단계의 표 후보, HWP 표 행) |
| `text` | 최종 문구. 이미지 영역은 VLM 판독 결과(OCR 원문이 아니다), HWP 구조 영역은 HWP 원문. 줄바꿈은 `\n` |
| `text_status` | `read`(text 문자열) · `no_text`(`""`, 글자 없음으로 판독) · `read_failed`(`null`, 판독 실패) · `not_read`(`null`, 판독 대상 아님) |
| `table` | 표 후보가 아니면 `null`. 표 후보면 아래 "표 추출 방식" |
| `review` | **파싱 검토** `{required, quality_reasons, context_reasons}`. `required` 는 둘 중 하나라도 있으면 `true`. 코드는 아래 표 |
| `candidate_boundary` | 미검증 후보 경계 타일의 영역 |
| `product_assignment` | 영역이 속한 상품(아래) |
| `labeling` | 영역의 상품별 라벨(아래) |
| `semantic_review` | **의미 검토** `{required, codes}`. 소속·라벨 단계의 코드(아래 표). 파싱 검토와 따로 둔다 |

**review.quality_reasons**(판독 신뢰. 문구를 그대로 믿기 어려운 이유)

| 코드 | 뜻 |
|---|---|
| `read_failed` / `contract_error` / `empty_but_ocr_has_text` | 판독 실패 / 응답이 계약(스키마·ID)에 어긋남 / OCR 은 글자가 있는데 판독 문구가 빔 |
| `ocr_vlm_mismatch` / `ocr_hwp_mismatch` | OCR 글자와 판독 문구가 서로 덮는 비율이 문턱 미만(HWP 구조 영역은 원문 대 렌더 OCR) |
| `digit_mismatch` | OCR 과 판독 문구의 숫자 덩어리 집합이 다름(천 단위 쉼표만 붙이고 쉼표·점·띄어쓰기는 구분자). HWP 구조 영역은 맥락 사유 `ocr_hwp_digit_mismatch` 로 둔다 |
| `vlm_uncertain` | 모델이 확신하지 못한다고 답함 |
| `text_from_outside_box` / `repeated_text` / `reading_order_conflict` | 상자 밖 OCR 문구가 들어감 / 같은 문구 반복 / 단순 세로 배치인데 줄 순서가 위→아래와 어긋남 |
| `special_token_in_text` / `output_artifact_in_text` | 모델 내부 토큰·응답 구조 잔여물이 문구에 섞임 |
| `reread_conflict` / `reread_failed` / `reread_outside_text` / `reread_outside_lines` / `reread_outside_line_count` / `reread_text_salvaged` | 재판독 후보 선택 사유(재판독과 1차가 다름, 재판독 실패, 재판독이 다른 영역 글자를 가져옴, 잘린 응답에서 살림) |
| `patch_not_applied` | 보완 판독의 수정 일부를 적용하지 못함 |
| `table_rows_missing` / `table_rows_incomplete` | 표인데 행이 없음 / 행이 문구를 덜 덮음 |
| `table_structure_failed` / `table_text_rows_from_different_reads` | 셀 구조 응답이 계약에 어긋남 / 표 문구와 행이 서로 다른 판독에서 옴 |

**review.context_reasons**(경계·보충·겹침 같은 맥락. 문구 자체의 오류 근거는 아니다)

| 코드 | 뜻 |
|---|---|
| `ocr_crosses_region_boundary` / `region_overlap` / `overlap_duplicate_text` | OCR 줄이 영역 경계에 걸침 / 영역이 다른 영역과 겹침 / 겹친 영역과 같은 문구 |
| `candidate_boundary_unvalidated` | 미검증 후보 경계 타일의 영역 |
| `supplement_region`, `supplement_*` | 놓친 영역 보충으로 추가한 영역과 그 방식 |
| `table_candidate_rejected` / `text_from_table_rows` / `layout_table_read_as_text` | 표 후보를 표가 아니라고 판정 / 문구가 비어 표 행을 이어 문구로 씀 / 문서 틀 표를 글 영역으로 판독 |
| `hwp_structure_text` / `ocr_hwp_digit_mismatch` | HWP 원문 영역 / HWP 원문과 렌더 OCR 의 숫자가 다름(OCR 쪽 차이) |
| `no_ocr_reference` / `page_output_partial` | 대조할 OCR 이 없음 / 페이지 판독 응답이 한도에서 잘려 끝까지 쓴 영역만 씀 |

**product_assignment** `{status, scope, product_ids, basis}`

| status · scope | 뜻 | product_ids |
|---|---|---|
| `completed` · `specific` | 한 상품 전용 영역 | 1개 |
| `completed` · `shared` | 여러 상품에 공통(회사명·머리글·고지·심의번호 등) | 1개 이상 |
| `completed` · `mixed` | 한 영역 안에 상품별 내용이 섞임 | 2개 이상 |
| `partial` · `unknown` | 소속 미확정. 라벨링하지 않는다(`labeling.status=not_run`) | 빈 배열 |
| `failed` · `not_run` | 분류 호출 실패·미실행. `scope` 는 `null` | 빈 배열 |

`basis` 는 `model`(모델 배정), `single_product`(후보 상품이 하나라 호출 없이 배정, scope 는 `specific`), `null`(미실행·실패·광고 상품 없음)이다.

**labeling** `{status, items[{product_id, label_ids}]}`: 배정된 상품마다 항목이 하나 있고 `label_ids` 는 그 상품 템플릿의 라벨 스키마에 있는 구분값 이름(예: `심의번호`, `가입기간`, `금리`)이다. 상품이 둘 이상인 영역은 상품별로 따로 라벨링하므로 같은 영역도 상품마다 라벨이 다를 수 있다. `completed` 에서 `label_ids` 가 빈 배열이면 "해당 라벨 없음"으로 판정한 것이고, `not_run`·`failed` 의 빈 `items` 와 다르다(사유는 `semantic_review.codes`). `partial` 은 일부 상품만 라벨이 정해진 경우다.

**semantic_review.codes**

| 코드 | 뜻 |
|---|---|
| `assignment_unknown` / `assignment_invalid` / `target_without_evidence` | 소속 미확정 / 응답의 배정이 대상과 맞지 않음 / 근거 없는 대상만 가리킴 |
| `assignment_failed` / `assignment_missing` | 관측 요청 실패 / 응답에 영역 없음 |
| `shared_without_product` / `mixed_parts_missing` | 공통 영역인데 같은 페이지·파일에 상품 없음 / 혼합 영역의 상품별 인용이 없거나 문구와 다름 |
| `label_failed` / `label_missing` / `label_quote_rejected` | 라벨 호출 실패 / 응답에 영역 없음 / 인용이 영역 문구·표 셀에 없어 버린 라벨 있음 |
| `no_final_text` / `product_not_classified` | 최종 문구 없음 / 배정 상품이 미지원·미확정이라 라벨링 안 함 |

### 표 추출 방식

표는 **판독 문구(`text`)와 셀 구조(`table.cells`)를 같은 응답 하나로** 받는다. 셀을 이어 문구를 만들거나 문구를 잘라 셀을 만들지 않는다.

1. **표 후보 찾기**: 레이아웃(PP-StructureV3)이 표로 검출한 상자가 표 후보다(`table_candidate: true`). 판독 뒤 어느 영역에도 들지 않은 OCR 글자가 있는 페이지는 표 문턱을 0.3 으로 낮춘 OCR 을 한 번 더 받아 표 상자를 찾고, 남은 글자 묶음이 2행×2열 이상이면 표 후보로 보충한다(`-S` 영역). 페이지 대부분을 덮는 문서 틀 표는 글 영역으로 판독한다(`layout_table_read_as_text`).
2. **1차 판독**: 페이지 판독에서 표 후보도 다른 영역과 함께 문구와 `is_table`·행을 받는다.
3. **셀 재판독(table-cells/1)**: 표 후보마다 따로 한 번 더 읽는다. 입력은 표 영역 원본 crop(표시 없음)과 그 영역이 속한 판독 단위의 문맥 이미지, 영역 OCR 후보(참고)다. 응답은 `{is_table, text, n_rows, n_cols, cells[{row, col, rowspan, colspan, text}]}` 이다.
   - `row`·`col` 은 0부터 세는 논리 칸 위치(픽셀 아님), 머리글 행도 행이다.
   - 병합 칸은 `rowspan`·`colspan` 으로 쓰고 문구는 시작 칸(가장 위·왼쪽)에 한 번만 쓴다.
   - 격자의 모든 칸은 정확히 한 셀이 덮는다. 글자 없는 빈 칸은 `text: ""` 셀이다.
   - 칸 안의 여러 줄은 공백 하나로 잇는다. 표 밖 제목·캡션·각주는 셀로 만들지 않고 `text` 에만 쓴다.
   - `text` 는 영역 안 모든 문구다(표 부분은 위 행부터, 행 안은 왼쪽 칸부터).
4. **검사와 채택**: 셀이 격자를 빠짐없이 한 번씩 덮는지, 범위·중복·타입을 검사한다(`check_cells`). 통과하면 그 응답의 `text`·`cells` 를 쓰고 `rows` 는 `cells` 에서 만든다(병합 칸이 덮는 나머지 위치는 `""`). 통과하지 못하면 1차 판독의 문구·행을 그대로 두고 `table_structure_failed` 를 붙인다(`--table-text-fallback` 을 켜면 셀만 실패하고 문구가 온전할 때 그 문구를 쓰고 `structure: "text-only"`. 기본값·서비스는 끔).
5. **표가 아니라고 답한 경우**(`is_table: false`): `status: "not_table"`, `rows`·`cells` 는 `null`. 문구 글자는 셀 재판독 것을 쓰고, 줄바꿈 위치는 1차 판독에서 옮긴다(셀 재판독은 행을 한 줄로 이어 쓰는 경향이 있어, 유의사항 `·` 항목 줄이 한 줄로 합쳐지는 것을 막는다. 공백이 있던 자리만 바꾸고 글자는 그대로, `trace` 에 `line_breaks_from_first_read`).
6. **HWP·HWPX**: 표 행은 HWP 원문의 DOM 행이 구조 영역 하나가 되고 셀 구조는 그 행 한 줄이다(`structure: "hwp-dom-row/1"`, `n_rows: 1`). VLM 으로 읽지 않는다.

`table` 필드 값

| 필드 | 값 |
|---|---|
| `status` | `table`(표) · `not_table`(표 후보였지만 표가 아님) · `unknown`(표 판정 없음) |
| `rows` | 문자열 행 배열(셀에서 만든 보기). `table` 이 아니면 `null` |
| `structure` | `table-cells/1`(셀 재판독) · `hwp-dom-row/1`(HWP 표 행) · `text-only`(문구만 채택, 셀 실패) · `rows/legacy`(이전 경로 결과) |
| `n_rows`, `n_cols`, `cells` | 셀 구조. `cells` 가 원본이고 `rows` 는 보기다. 표가 아니면 `n_rows=0`·`n_cols=0`·`cells=null` |

**예시 1. 병합 칸이 있는 표**(전량 테스트 w057 적금 광고의 금리표, 2026-10-29 실행). 셀 9개가 3행×4열 격자를 빠짐없이 덮고, 마지막 행 `비고` 는 4칸을 합친 칸이다(`colspan: 4`). `rows` 에서는 병합 칸이 덮는 나머지 위치가 `""` 다. `text` 는 같은 응답의 판독 문구로, 셀을 이어 만든 것이 아니다.

```jsonc
// layout-result/3 regions[] 한 항목
{
  "region_id": "w057-2_p01-v0-L001", "page_id": "w057-2_p01",
  "bbox": [215.65, 826.03, 811.97, 909.0], "table_candidate": true,
  "text": "이자지급방식 가입기간(회전기간*) 기본금리(%) 우대금리(%p) 만기일시지급식 12개월이상 ~ 24개월미만 2.300 - 비고",
  "text_status": "read",
  "table": {
    "status": "table", "structure": "table-cells/1", "n_rows": 3, "n_cols": 4,
    "cells": [
      {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "이자지급방식"},
      {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "가입기간(회전기간*)"},
      {"row": 0, "col": 2, "rowspan": 1, "colspan": 1, "text": "기본금리(%)"},
      {"row": 0, "col": 3, "rowspan": 1, "colspan": 1, "text": "우대금리(%p)"},
      {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "만기일시지급식"},
      {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "12개월이상 ~ 24개월미만"},
      {"row": 1, "col": 2, "rowspan": 1, "colspan": 1, "text": "2.300"},
      {"row": 1, "col": 3, "rowspan": 1, "colspan": 1, "text": "-"},
      {"row": 2, "col": 0, "rowspan": 1, "colspan": 4, "text": "비고"}          // 4칸 병합, 문구는 시작 칸에 한 번
    ],
    "rows": [["이자지급방식", "가입기간(회전기간*)", "기본금리(%)", "우대금리(%p)"],
             ["만기일시지급식", "12개월이상 ~ 24개월미만", "2.300", "-"],
             ["비고", "", "", ""]]                                                // cells 에서 만든 보기
  },
  "review": {"required": false, "quality_reasons": [], "context_reasons": []},
  "candidate_boundary": false,
  "product_assignment": {"status": "completed", "scope": "specific", "product_ids": ["product_001"], "basis": "model"},
  "labeling": {"status": "completed", "items": [{"product_id": "product_001", "label_ids": ["이자지급시기", "가입기간", "금리", "우대금리"]}]},
  "semantic_review": {"required": false, "codes": []}
}
```

같은 영역이 심의 단계 입력(review-input/2)에서는 이렇게 넘어간다. `rows` 는 넣지 않고 셀 구조만 넘긴다.

```jsonc
{
  "region_id": "w057-2_p01-v0-L001", "bbox": [215.65, 826.03, 811.97, 909.0],
  "text": "이자지급방식 가입기간(회전기간*) 기본금리(%) 우대금리(%p) 만기일시지급식 12개월이상 ~ 24개월미만 2.300 - 비고",
  "text_status": "read", "text_source": "vlm",
  "is_table": true,
  "table": {"n_rows": 3, "n_cols": 4, "cells": [ /* 위 9개 셀 그대로 */ ]},
  "product": {"scope": "specific", "product_ids": ["product_001"]},
  "labels": [{"product_id": "product_001", "label_ids": ["이자지급시기", "가입기간", "금리", "우대금리"]}],
  "quality_reasons": [], "semantic_codes": [], "needs_review": false
}
```

**예시 2. 표 후보였지만 표가 아닌 영역**(전량 테스트 dm05 입출식 상품 페이지의 "기타사항" 칸, 2026-10-29 서비스 실행). 레이아웃은 표로 검출했지만 셀 재판독이 표가 아니라고 답했다. `table` 은 `not_table` 이고 셀이 없으며, 문구는 셀 재판독의 글자에 1차 판독의 줄바꿈 위치를 옮긴 것이다(줄 7개를 옮김, 원본처럼 `·` 항목마다 줄이 나뉜다). 심의 입력에서는 `is_table: false`, `table: null` 이다.

```jsonc
{
  "region_id": "dm05_p02-v0-L001", "table_candidate": true, "text_status": "read",
  "text": "무통장거래 · 가능\n휴면예금 및 출연 · 이자지급을 포함한 … 휴면예금을 찾으실 수 있습니다.\n· 휴면예금은 휴면예금통합조회서비스(www.sleepmoney.or.kr)를 통해 조회가능합니다.\n거래중지계좌 이 예금이 다음 각 호의 1에 해당할 때에는 …\n· 1. 예금잔액이 1만원 미만이며, 1년 이상 입출금 거래가 없는 계좌\n· 2. …",
  "table": {"status": "not_table", "rows": null, "structure": "table-cells/1", "n_rows": 0, "n_cols": 0, "cells": null},
  "review": {"required": true,
             "quality_reasons": ["digit_mismatch"],                      // OCR 은 '1397' 을 '139' 로 읽음(판독은 1397)
             "context_reasons": ["table_candidate_rejected", "region_overlap", "ocr_crosses_region_boundary"]}
}
```

### unassigned_texts[]

어느 영역에도 면적 50% 이상 들어가지 않은 OCR 줄이다(영역 밖 문구 등). `{ocr_id, page_id, bbox, ocr_text, reason, candidate_region_ids, empty_text, final}` 이고 `final` 은 항상 `false` 다. 최종 문구가 아니고 영역 `text` 에 합치지 않는다. `reason` 은 `outside_layout`(영역 밖) · `partial_overlap`(영역 안 비율 50% 미만) · `ambiguous_*` 이다.

### 최종 결과 예시(실제 실행 결과에서 발췌)

전량 테스트 mx1306(입출식 통장·적립식 적금 혼합 광고, PDF 1쪽)을 파서 서비스로 실행한 결과(2026-10-29, 등록 템플릿 2개 = `restricted`)다. 영역 22개 중 3개만 옮겼고 bbox 는 소수 둘째 자리까지 줄여 적었다.

```jsonc
{
  "contract": "layout-result/3",
  "result_id": "lr3-97b4beadccb9ddb82e80",
  "submission_id": "mx1306",
  "source": {"file_id": "mx1306", "file_name": "13. 예금성상품-입출식, 적립식 혼합 광고 6.pdf", "input_order": 1,
             "file_type": "pdf", "sha256": "5056ceda…", "page_count": 1},
  "parse": {"contract": "layout-result/2", "result_id": "lr2-1b75452a030df838cb61", "profile": "page-first-default/1",
            "path": "../../../files/mx1306/result.json"},
  "semantic": {
    "profile": "semantic-observe/1", "classification_mode": "observe", "status": "completed",
    "versions": {"observe": "observe/7", "target_rules": "unnamed-target-rules/2", "decisive": "decisive-signal/1",
                 "first_input": "first-input/1", "integrate": "integrate/4", "label": "label/3",
                 "template_request": "template-request/1", "deposit_marks": "deposit-marks/1",
                 "assets": "nh-label-schema@358baf3+policy", "policy": "classification-policy/4", "assets_digest": "4bf35d0b21e798ba"},
    "template_request": {"template_ids": ["예금성상품-입출식", "예금성상품-적립식"], "mode": "restricted"}
  },
  "pages": [{
    "page_id": "mx1306_p01", "page_no": 1, "width": 1654, "height": 2339, "coordinate_system": "page_px_xyxy",
    "image": {"sha256": "fbb204ff…", "path": "../../../stage1/pages/mx1306_p01.png"},
    "route": "whole", "execution_status": "READY", "quality_status": "not_verified", "boundary": null,
    "issues": [
      {"code": "TEXT_OUTSIDE_LAYOUT", "severity": "review", "message": "원래 bbox 가 어떤 영역과도 겹치지 않는 OCR 글자 2개"},
      {"code": "TEXT_PARTIAL_IN_REGION", "severity": "review", "message": "영역 경계에 걸쳐 일부만 포함된 OCR 글자 21개"},
      {"code": "REGION_COVERS_LARGE_AREA", "severity": "info", "message": "페이지 면적 25% 이상 영역 1개(과대 컨테이너 여부 확인)"}
    ]
  }],
  "products": [
    {"product_id": "product_001", "name": "NH올원e통장", "product_group": "예금성", "status": "classified",
     "templates": [{"template_id": "예금성상품-입출식", "role": "product", "label_schema": "deposit-demand"}],
     "pension": null, "files": ["mx1306"], "pages": ["mx1306_p01"], "review": {"required": false, "codes": []}},
    {"product_id": "product_002", "name": "NH올원e적금", "product_group": "예금성", "status": "classified",
     "templates": [{"template_id": "예금성상품-적립식", "role": "product", "label_schema": "deposit-saving"}],
     "pension": null, "files": ["mx1306"], "pages": ["mx1306_p01"], "review": {"required": false, "codes": []}}
  ],
  "regions": [
    {   // 한 상품 전용 영역
      "region_id": "mx1306_p01-v0-L013", "page_id": "mx1306_p01", "bbox": [233.73, 1574.11, 690.52, 1608.41],
      "table_candidate": false, "text": "기본금리 연0.1%(2026.08.21.기준,세전)", "text_status": "read", "table": null,
      "review": {"required": true, "quality_reasons": [], "context_reasons": ["ocr_crosses_region_boundary"]},   // 맥락 사유만 있음
      "candidate_boundary": false,
      "product_assignment": {"status": "completed", "scope": "specific", "product_ids": ["product_001"], "basis": "model"},
      "labeling": {"status": "completed", "items": [{"product_id": "product_001", "label_ids": ["금리"]}]},
      "semantic_review": {"required": false, "codes": []}
    },
    {   // 두 상품 공통 영역: 상품마다 라벨링
      "region_id": "mx1306_p01-v0-L001", "page_id": "mx1306_p01", "bbox": [401.71, 2129.54, 1369.82, 2248.83],
      "table_candidate": false,
      "text": "■이 예금은 예금자보호법에 따라 원금과 소정의 이자를 합하여 1인당 “1억원까지”(본 은행의 여타 보호상품과 합산) 보호됩니다.\n■계좌에 압류, 가압류, 질권설정 등이 등록될 경우 원금 및 이자 지급을 제한하며, …\n■기타 자세한 내용은 … 고객행복센터(1661-3000, 1522-3000)로 문의하시기 바랍니다.\n■준법감시인 심의필 2026-0000(2026.00.00.~0000.00.00.) [상품담당부서 : 개인고객부 / 제작부서 : 영업채널전략부]",
      "text_status": "read", "table": null,
      "review": {"required": true, "quality_reasons": [], "context_reasons": ["ocr_crosses_region_boundary"]},
      "candidate_boundary": false,
      "product_assignment": {"status": "completed", "scope": "shared", "product_ids": ["product_001", "product_002"], "basis": "model"},
      "labeling": {"status": "completed", "items": [
        {"product_id": "product_001", "label_ids": ["예금자보호", "이자지급제한", "유의사항", "심의번호"]},
        {"product_id": "product_002", "label_ids": ["예금자보호", "이자지급제한", "유의사항", "심의번호"]}]},
      "semantic_review": {"required": false, "codes": []}
    },
    {   // 예금자보호 표시(로고) 영역: deposit_insurance_marks.protected.region_ids 가 가리킨다
      "region_id": "mx1306_p01-v0-L015", "page_id": "mx1306_p01", "bbox": [220.31, 2136.01, 390.38, 2242.66],
      "table_candidate": false, "text": "예금보험공사\n보호금융상품\n1인당 최고 1억원", "text_status": "read", "table": null,
      "review": {"required": false, "quality_reasons": [], "context_reasons": []},
      "candidate_boundary": false,
      "product_assignment": {"status": "completed", "scope": "shared", "product_ids": ["product_001", "product_002"], "basis": "model"},
      "labeling": {"status": "completed", "items": [{"product_id": "product_001", "label_ids": ["예금자보호"]},
                                                   {"product_id": "product_002", "label_ids": ["예금자보호"]}]},
      "semantic_review": {"required": false, "codes": []}
    }
    // … 나머지 19개 영역
  ],
  "unassigned_texts": [],
  "deposit_insurance_marks": {"version": "deposit-marks/1",
                              "protected": {"status": "PRESENT", "code": null, "region_ids": ["mx1306_p01-v0-L015"]},
                              "non_protected": {"status": "ABSENT", "code": null, "region_ids": []}},
  "trace_ref": {"parse_result": "../../../files/mx1306/result.json", "semantic_trace": "../../trace/semantic/mx1306.json"}
}
```

### 서비스 실행 결과(service.json)

`scripts/run_service.py --request <요청 json> --out <폴더>` 의 결과 폴더 맨 위에 쓴다. nh-ad-compliance 파서 서비스는 이 폴더를 ZIP 으로 돌려준다.

| 필드 | 값 |
|---|---|
| `contract` | `"parser-service/1"` |
| `status` | `completed` · `partial`(의미 단계 일부만 정해짐, 심의 입력 있음) · `failed`(심의 입력 없음) |
| `failure` | 실패면 `{code, message}`, 아니면 `null`. 코드와 종료 코드·서비스 HTTP 상태: `INPUT_INVALID`(2, 422 다시 시도하지 않음) · `PAGES_NOT_PROCESSED`·`READ_FAILED`·`SEMANTIC_FAILED`(3, 503 OCR·VLM 장애, 다시 시도) · `CONTRACT_CHECK_FAILED`(4, 502) · `INTERNAL_ERROR`(1, 502) |
| `review_input` | 심의 입력 경로(`semantic/handoff/<submission_id>.json`). 실패면 `null` |
| `contracts` | `{result: "layout-result/3", review_input: "review-input/2"}` |
| `request` | `{submission_id, files[{file_id, file_name}], template_request{template_ids, mode}}` |
| `files[]`, `pages[]` | 파일별 최종 결과 경로·의미 단계 상태, 쪽별 `execution_status` |
| `regions`, `checks`, `timing` | 영역 수·판독 실패 수, 계약 검사 통과 여부(`parse`·`semantic`·`review_input`), 전체·의미 단계 시간(초) |

```jsonc
// 위 mx1306 실행의 service.json
{
  "contract": "parser-service/1", "status": "completed", "failure": null,
  "review_input": "semantic/handoff/mx1306.json",
  "contracts": {"result": "layout-result/3", "review_input": "review-input/2"},
  "request": {"submission_id": "mx1306", "files": [{"file_id": "mx1306", "file_name": "13. 예금성상품-입출식, 적립식 혼합 광고 6.pdf"}],
              "template_request": {"template_ids": ["예금성상품-입출식", "예금성상품-적립식"], "mode": "restricted"}},
  "files": [{"file_id": "mx1306", "file_name": "13. 예금성상품-입출식, 적립식 혼합 광고 6.pdf",
             "result": "semantic/files/mx1306/result.json", "semantic_status": "completed"}],
  "pages": [{"page_id": "mx1306_p01", "file_id": "mx1306", "page_no": 1, "execution_status": "READY"}],
  "regions": {"total": 22, "read_failed": 0},
  "checks": {"parse": true, "semantic": true, "review_input": true},
  "timing": {"end_to_end_wall_seconds": 134.99, "semantic_stage_wall_seconds": 27.782}
}
// OCR 이 안 될 때(실제 2026-10-29 OCR 서버 접속 시간 초과): 종료 코드 3, 서비스 503
// {"contract": "parser-service/1", "status": "failed", "review_input": null,
//  "failure": {"code": "PAGES_NOT_PROCESSED", "message": "dm05_p01=OCR_FAILED, dm05_p02=OCR_FAILED"}, …}
```

## 심의 단계 입력(review-input/2)

묶음의 파일별 최종 결과(layout-result/3)에서 심의에 필요한 값만 추린 **묶음 하나에 JSON 하나**다. 값은 layout-result/3 그대로이며(영역 ID·bbox·text·표 셀·소속·라벨) 새로 판단하지 않는다. 경로는 이 파일 위치 기준 상대 경로이고, 스키마는 `schemas/review-input-2.schema.json`(handoff check 가 검사)이다.

**만드는 방법**(`layout_pipeline/handoff.py`)

- 파일은 `input_order` 순, 페이지는 파일 안 순서대로 이어 `seq`(묶음 안 쪽 연번, 1부터)를 붙인다.
- 페이지 안 영역은 읽기 순서(위→아래, 같은 줄은 왼쪽→오른쪽)로 놓는다. 영역 집합은 최종 결과와 같다(빠짐·추가 없음, check 가 확인).
- 상품은 묶음 전체에서 `product_id` 순으로 한 번씩, 템플릿마다 그 템플릿에서 쓸 수 있는 라벨 목록(`labels`)을 붙인다.

| 필드 | 값 | 만드는 곳(layout-result/3) |
|---|---|---|
| `contract`, `submission_id` | `"review-input/2"`, 묶음 ID | `submission_id` |
| `template_request` | `{template_ids, mode}` 등록 템플릿 요청 그대로 | `semantic.template_request` |
| `files[]` | `{file_id, file_name, file_type, input_order, page_count, hwp_review_html}`. `hwp_review_html` 은 HWP·HWPX 원문 재구성 검토 HTML 경로, 그 밖에는 `null` | `source` |
| `products[]` | `{product_id, name, product_group, status, templates[{template_id, role, label_schema, labels}], pension, files, pages}` | `products[]`(+ 라벨 스키마의 `labels`) |
| `deposit_insurance_marks` | 최종 결과와 같음. 이 기능 이전 결과면 `UNKNOWN`·`NOT_CHECKED` | `deposit_insurance_marks` |
| `pages[]` | `{page_id, file_id, page_no, seq, width, height, image{path, sha256}, processed, regions[]}`. `processed` 는 `execution_status` 가 `READY`·`CANDIDATE_RUN` 이면 `true` | `pages[]` |
| `source` | `{<file_id>: <layout-result/3 result_id>}` | `result_id` |

`pages[].regions[]`

| 필드 | 값 | 만드는 곳 |
|---|---|---|
| `region_id`, `bbox`, `text`, `text_status` | 최종 결과 그대로. `bbox` 는 그 페이지 이미지(`width`×`height`) 픽셀 | 같은 이름 |
| `text_source` | `vlm`(이미지 판독) · `hwp`(HWP 원문, `-h0-H` 영역) · `null`(문구 없음) | `region_id`·`text` |
| `is_table` | `true`(table.status=table) · `false`(표 후보 아님 또는 not_table) · `null`(표 판정 불가, unknown) | `table.status` |
| `table` | `{n_rows, n_cols, cells[{row, col, rowspan, colspan, text}]}`. 표가 아니거나 셀 구조가 없으면 `null`. 표의 `text` 는 판독 문구 그대로(셀로 바꾸지 않음) | `table.cells` |
| `product` | `{scope, product_ids}` | `product_assignment` |
| `labels` | `[{product_id, label_ids}]` 상품별 라벨 | `labeling.items` |
| `quality_reasons` | 판독 품질 사유. 최종 결과 `review.quality_reasons` + `read_failed`(판독 실패) + `table_structure_missing`(표인데 셀 구조 없음) | `review.quality_reasons` |
| `semantic_codes` | 소속·라벨 단계 코드 | `semantic_review.codes` |
| `needs_review` | `quality_reasons` 나 `semantic_codes` 가 있으면 `true`(맥락 사유는 넣지 않는다) | |

넣지 않는 것: 실행·버전 정보, 경계·진단·이슈, 맥락 검토 사유(`context_reasons`), 미배정 OCR, 판단 근거. 필요하면 `source` 의 result_id 로 layout-result/3 을 찾는다.

**예시**(위 mx1306 실행의 `semantic/handoff/mx1306.json`, 영역 22개 중 3개)

```jsonc
{
  "contract": "review-input/2", "submission_id": "mx1306",
  "template_request": {"template_ids": ["예금성상품-입출식", "예금성상품-적립식"], "mode": "restricted"},
  "files": [{"file_id": "mx1306", "file_name": "13. 예금성상품-입출식, 적립식 혼합 광고 6.pdf", "file_type": "pdf",
             "input_order": 1, "page_count": 1, "hwp_review_html": null}],
  "products": [
    {"product_id": "product_001", "name": "NH올원e통장", "product_group": "예금성", "status": "classified",
     "templates": [{"template_id": "예금성상품-입출식", "role": "product", "label_schema": "deposit-demand",
                    "labels": ["회사명", "상품명", "가입대상", "금리", "우대금리", "이자지급시기", "이자지급제한", "예금자보호", "유의사항", "심의번호"]}],
     "pension": null, "files": ["mx1306"], "pages": ["mx1306_p01"]},
    {"product_id": "product_002", "name": "NH올원e적금", "product_group": "예금성", "status": "classified",
     "templates": [{"template_id": "예금성상품-적립식", "role": "product", "label_schema": "deposit-saving",
                    "labels": ["회사명", "상품명", "가입대상", "가입금액", "가입기간", "금리", "우대금리", "예상수취이자",
                               "중도해지이율", "만기후이율", "이자지급시기", "이자지급제한", "예금자보호", "유의사항", "심의번호"]}],
     "pension": null, "files": ["mx1306"], "pages": ["mx1306_p01"]}
  ],
  "deposit_insurance_marks": {"version": "deposit-marks/1",
                              "protected": {"status": "PRESENT", "code": null, "region_ids": ["mx1306_p01-v0-L015"]},
                              "non_protected": {"status": "ABSENT", "code": null, "region_ids": []}},
  "pages": [{
    "page_id": "mx1306_p01", "file_id": "mx1306", "page_no": 1, "seq": 1, "width": 1654, "height": 2339,
    "image": {"path": "../../stage1/pages/mx1306_p01.png", "sha256": "fbb204ff…"}, "processed": true,
    "regions": [                                   // 읽기 순서: L000, L012, L006, L010, L008, L003, L013, …, L001, L015, L004
      {"region_id": "mx1306_p01-v0-L000", "bbox": [56.84, 40.47, 1653.84, 1271.74],
       "text": "금융의 모든 순간\nNH농협금융\nNH 올원e\n통장·적금\nNH 비대면 전용 입출식 및 적립식\n고금리 제공\n수수료 면제\n(2026.00.00.~0000.00.00.)\n은행연합회 심의필 제 2026-00000호",
       "text_status": "read", "text_source": "vlm", "is_table": false, "table": null,
       "product": {"scope": "shared", "product_ids": ["product_001", "product_002"]},
       "labels": [{"product_id": "product_001", "label_ids": ["심의번호"]}, {"product_id": "product_002", "label_ids": ["심의번호"]}],
       "quality_reasons": ["reread_outside_text", "text_from_outside_box"],   // 판정에서 이 영역은 판독 확인 필요
       "semantic_codes": [], "needs_review": true},
      {"region_id": "mx1306_p01-v0-L013", "bbox": [233.73, 1574.11, 690.52, 1608.41],
       "text": "기본금리 연0.1%(2026.08.21.기준,세전)", "text_status": "read", "text_source": "vlm", "is_table": false, "table": null,
       "product": {"scope": "specific", "product_ids": ["product_001"]},
       "labels": [{"product_id": "product_001", "label_ids": ["금리"]}],
       "quality_reasons": [], "semantic_codes": [], "needs_review": false},   // 최종 결과의 맥락 사유(경계 걸침)는 넣지 않음
      {"region_id": "mx1306_p01-v0-L015", "bbox": [220.31, 2136.01, 390.38, 2242.66],
       "text": "예금보험공사\n보호금융상품\n1인당 최고 1억원", "text_status": "read", "text_source": "vlm", "is_table": false, "table": null,
       "product": {"scope": "shared", "product_ids": ["product_001", "product_002"]},
       "labels": [{"product_id": "product_001", "label_ids": ["예금자보호"]}, {"product_id": "product_002", "label_ids": ["예금자보호"]}],
       "quality_reasons": [], "semantic_codes": [], "needs_review": false}
      // … 나머지 19개 영역
    ]
  }],
  "source": {"mx1306": "lr3-97b4beadccb9ddb82e80"}
}
```

표 영역이 넘어가는 모양은 위 "표 추출 방식"의 예시 1·2 다.

**nh-ad-compliance 가 읽는 방식**(그쪽 `nh_ad_parser_contracts.review_input`, ADR-0096): 작업기와 백엔드가 review-input/2 에서 판정용 보기(`nh-ad-review-input-view-v1`)를 같은 방식으로 만든다. `page_no` 는 `seq`, 영역 소속은 판정 상품 하나면 그 상품·판정 상품 모두의 공통이면 `page_common`·일부 상품만의 공통이면 첫 상품 + `shared_with_products`, 라벨은 상품별 라벨의 합집합, 판정의 판독 불가 여부(`needs_review`)는 `quality_reasons` 만 본다. 표는 판독 문구와 셀을 그대로 넘기고 bbox·문구는 바꾸지 않는다.

### 읽을 때 주의

- `review.required=false`, `semantic_review.required=false`, `needs_review=false` 는 사람이 승인했다는 뜻도 정확도 보장도 아니다. 검토 근거가 없다는 뜻이다. 근거는 영역별 `quality_reasons`·`context_reasons`·`codes` 에 있다.
- 계약 검사(`contract-check.json`, `handoff/check.json`)는 스키마·참조·파싱 값 보존·라벨 인용 위치만 확인한다. 상품 분류·템플릿·라벨 내용이 맞는지는 검사하지 않는다.
- 긴 이미지가 `CANDIDATE_RUN` 이면(자동 경계가 검증 범위 폭 640~700px 밖) 그 영역에는 `candidate_boundary_unvalidated` 가 붙고 상품에는 `candidate_pages` 가 붙는다. 확정 결과가 아니다.
- 그래픽 글자(배너 속 장식 숫자 등)는 판독에서 빠질 수 있다(2026-10-29 판정 비교의 "0%" 장식, docs/22). 영역 밖 OCR 은 `unassigned_texts` 에만 있다.
- 프롬프트, 라벨·상품 근거 인용, 특징 ID, 원시 응답, 중간 판독 후보, 임계값, 시간은 최종 JSON 에 없고 `trace/` 에 있다.
- 검토 코드의 정의와 이력은 `docs/12-file-result-contract.md`(파싱), `docs/14-semantic-labeling.md` §3(의미), `docs/21-parser-service-integration.md`(등록 템플릿·예금자보호·서비스)에 있다. layout-result/2 는 `parser_profile` 을 갖고 `products[]` 가 `{product_id, name}` 참조뿐이며 `semantic`·`semantic_review`·`basis` 가 없다.
