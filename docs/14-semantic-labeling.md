# 14. 의미 단계: 상품·템플릿 분류 · 영역 상품 배정 · 라벨링(layout-result/3)

지시: 2026-10-27 사용자 프롬프트(광고 묶음 상품·템플릿 자동 분류, 상품 구분, 영역 배정, 템플릿 라벨 스키마 다중 라벨링, 최소 최종 JSON, 결과 화면).

> 2026-10-27 이후 기본 분류 방식은 **docs/15(inventory-first: 묶음 전체 상품 목록을 먼저 확정 → 그 목록 안에서 배정)** 이다. 이 문서의
> 관측·통합(observe/integrate)은 `--classification-mode legacy` 로 남아 이전 결과 재구성·비교에 쓴다. 라벨링·검사·자산은 두 방식이 같다.
> layout-result/3 최종 구조(classification_mode·products[].pages·product_assignment.basis 추가)는 docs/15 §4.

**하지 않은 것**: nh-ad-compliance 연동, 심의 적정/부적정 판정, 파싱 알고리즘·OCR bbox·표 판독·재판독·해상도·타일 정책 변경, GPU 서버 설정 변경. 파싱 snapshot(layout-result/2)의 값은 바꾸지 않는다.

## 1. 처리 순서와 코드 위치

| 단계 | 내용 | 코드 |
|---|---|---|
| 입력 | 저장된 파일별 파싱 결과(layout-result/2)를 제출 묶음별로 읽는다(읽기만). OCR·전사 호출 없음 | `semantic.load_parse` |
| 별칭 | 파일 F1…, 페이지 P1…(입력 순서→쪽), 영역 P1-R01…(페이지 안 읽기 순서). 파일명·file_id·submission_id·manifest 설명·정답은 요청에 넣지 않는다 | `classify.build_bundle` |
| A 관측 | 영역 표시 페이지 사본(긴 이미지는 타일 구간) + 영역 목록(bbox·최종 text 생략 없이·기존 표 rows) + 분류 정책 → 광고 대상(이름·상품군·템플릿·근거 인용)·비대상 언급(역할)·영역 배정 | `classify.observe_request/validate_observe` |
| | 작은 묶음(쪽 ≤4·영역 ≤80·글자 ≤16,000·이미지 ≤6)은 묶음 전체 한 요청(A~C 한 번), 큰 묶음은 페이지마다 관측(병렬) | `classify.plan_units` |
| B 통합 | 페이지별 관측이 여럿이면 대상 T1…을 묶음 단위 상품으로 잇고 템플릿을 정한다(글자 요청, 이미지 없음) | `classify.integrate_request/validate_integrate` |
| C 배정 | 관측의 영역 배정을 상품으로 옮긴다(호출 없음). 공통 영역의 대상이 비면 같은 페이지→같은 파일→(상품이 하나뿐이면) 그 상품 | `classify.resolve` |
| | 상품 확정: 이름(영역 문구에 그대로 있을 때만)·템플릿·상품군·퇴직연금(포함/미노출/미확정·제도·운용상품)·검토 코드 | `classify.resolve/_pension/_template_codes` |
| D 라벨링 | 상품·템플릿이 정해진 뒤 그 상품의 라벨 스키마(허용 구분값·예시)만으로 영역마다 라벨을 모두. 상품별·영역 묶음(≤20영역·≤8,000자)별 병렬 | `labeling.plan_jobs/label_request/validate_labels` |
| E 검사 | 계약(JSON Schema)·참조·상태·허용 라벨·묶음 일관성·파싱 값 보존·라벨 인용 위치 | `contract_v3`, `semantic.check_all` |
| F 출력 | 파일별 layout-result/3, 목록, 대조 화면, trace, 보고서 | `file_result_v3`, `final_viewer.build_semantic_viewer` |

- 실행: `scripts/run_semantic.py`(의미 단계만, `--rebuild`·`--dry-run`), `scripts/run_pipeline.py`(파싱 snapshot 저장 뒤 기본 실행, `--no-semantic`).
- GPU 동시 호출: 파싱 단계의 `--vlm-concurrency`(기본 2) 하나를 의미 단계의 모든 호출(관측·통합·라벨)이 공유한다(`semantic.Gate`). 단계·묶음별 상한을 따로 두지 않는다. 보고서의 `peak_concurrency` 가 실제 최대 동시 호출 수다.
- 재구성: 요청마다 열쇠(`observe:<묶음>:U1`, `integrate:<묶음>`, `label:<묶음>:<상품>:<번호>`)로 저장한 응답을 `ReplayClient` 가 돌려준다. 프롬프트·스키마·이미지 해시가 저장 때와 다르면 멈춘다.

## 2. 데이터 자산(`assets/semantic/`)

| 파일 | 출처 | 용도 |
|---|---|---|
| `label-schemas/*.json` 10종 | nh-ad-compliance `parser-pipeline/nh_parser_fin/label_schema/schemas/`(커밋 358baf3, 2026-10-21) | 라벨 지식(구분값·템플릿 예시문구·확정 예시·반례). 대출 상품명 노출·대출모집인·예금성 4종·ISA 3종·퇴직연금 일반 |
| `ad_templates.json` | 같은 저장소 `templates/ad_templates.json`(996d112) | 템플릿 ID·구분값 대조(테스트) |
| `product-contexts-v1.json` | `packages/rule-engine/.../product-contexts-v1.json`(0633d91) | 퇴직연금 기본 템플릿 + 운용상품 FUND·ETF·ELB 템플릿 |
| `classification-policy.json` | 이 저장소(작업 지시 7절 식별 기준을 옮김) | 템플릿별 특징 ID·판단·제외·혼합, 비대상 역할, 퇴직연금 규칙, 검토용 결정 신호 |
| `manifest.json` | — | 원본 저장소 HEAD(b3b611c)·파일별 커밋·SHA-256. 로드할 때 해시가 다르면 멈춘다 |

- 가져오기·확인: `scripts/import_semantic_assets.py --source ../nh-ad-compliance`, `--check`, 분류 정책 수정 뒤 `--refresh-local`.
- 라벨 지식 글은 원본 `render` 와 같은 구성(구분값별 템플릿 예시문구·실제 광고 예시·반례)이지만 예시 출처 광고 이름(`source_ad`)을 넣지 않고 예시 문구를 자르지 않는다. 기존 파싱 파이프라인·소유권 후처리·본문 중간 생략·느슨한 인용 검증·재시도·규칙 라벨 추가는 가져오지 않았다.
- 퇴직연금 운용상품 ETF·ELB 전용 라벨 스키마는 원본에도 없다. 퇴직연금 상품 영역은 `investment-pension-general` 로만 라벨링하고 FUND·ETF·ELB 는 템플릿 연결(`role: component`, `label_schema: null`)만 한다.

## 3. 결과 계약 layout-result/3(`schemas/layout-result-3.schema.json`)

파일 하나 = 문서 하나(`<out>/files/<file_id>/result.json`). 파싱 값은 layout-result/2 그대로이고, 새 버전에서 바뀐 것은 아래뿐이다.

| 필드 | 내용 |
|---|---|
| `contract`, `result_id` | `layout-result/3`, `lr3-<20 hex>`(파싱 result_id + 의미 단계 프로필·자산·지시 버전 + 그 묶음 응답 해시) |
| `parse` | 근거 파싱 snapshot: `contract`(layout-result/2), `result_id`, `profile`, `path`(상대 경로). `parser_profile` 은 중복이라 넣지 않는다 |
| `semantic` | `profile`(semantic-default/1), `status`(completed/partial/failed), `versions`(observe·integrate·label 지시, 분류 정책, 자산 버전·digest) |
| `products[]` | `product_id`(product_001…, 묶음 안 첫 등장 순), `name`(영역 문구에 그대로 있는 상품명, 없으면 null), `product_group`, `status`(classified/undetermined/unsupported), `templates[]`({template_id, role product\|component, label_schema}), `pension`(null 또는 {investment_products included\|not_exposed\|undetermined, plans IRP/DC/DB, components FUND/ETF/ELB}), `files`(그 상품이 나오는 파일), `review`{required, codes}. 같은 묶음의 모든 파일에 같은 정의 |
| `regions[].product_assignment` | `{status, scope, product_ids}`. completed: specific(1개)·shared(공통, 1개 이상)·mixed(한 영역에 상품별 내용, 2개 이상). partial: unknown(미확정, 빈 배열). failed: 관측 실패(scope null). 영역을 나누지 않는다 |
| `regions[].labeling` | `{status, items[{product_id, label_ids}]}`. completed: 배정 상품마다 item, 빈 label_ids 는 '해당 라벨 없음' 판정. partial: 일부 상품만. failed: 라벨 호출 실패. not_run: 라벨링하지 않음(사유는 semantic_review) |
| `regions[].semantic_review` | `{required, codes}` 짧은 코드. 파싱 검토(`review`)와 별개이며 파싱 검토 표시는 그대로 둔다 |
| `trace_ref` | `parse_result`(layout-result/2 경로), `semantic_trace`(묶음 trace) |

넣지 않는 것: 근거 인용·특징 ID·프롬프트·원시 응답·설명문·평가 정답(→ `trace/semantic/`). 페이지 이미지 경로만 새 파일 위치 기준으로 다시 쓴다(같은 파일을 가리키는지 보존 검사).

### 검토 코드

| 대상 | 코드 | 뜻 |
|---|---|---|
| 영역 | `assignment_unknown` / `assignment_invalid` / `target_without_evidence` | 소속 미확정 / 응답의 배정이 대상과 맞지 않아 미확정 / 근거 없는 대상(등록 안 함)만 가리켜 미확정 |
| | `assignment_failed` / `assignment_missing` | 관측 요청 실패 / 응답에 영역 없음 |
| | `shared_without_product` / `mixed_parts_missing` | 공통 영역인데 같은 페이지·파일에 상품 없음 / 혼합 영역의 상품별 인용이 없거나 문구와 다름 |
| | `label_failed` / `label_missing` / `label_quote_rejected` | 라벨 호출 실패 / 응답에 영역 없음 / 인용이 영역 문구·표 셀에 없어 버린 라벨 있음 |
| | `no_final_text` / `product_not_classified` | 최종 text 없음(판독 실패) / 배정 상품이 미지원·미확정이라 라벨링 안 함 |
| 상품 | `template_evidence_missing` / `template_conflict` / `multi_template_product` / `template_group_conflict` | 템플릿 특징 인용 없음 / 함께 둘 수 없는 템플릿 / 예금성 템플릿 둘(보통 별도 상품) / 상품군 충돌 |
| | `broker_with_named_loan` / `broker_signal_unused` | 대출모집인 + 상품명 노출(지시대로 검토 표시) / 모집인 결정 신호가 문구에 있는데 모집인 템플릿 없음 |
| | `isa_types_split` | 같은 파일에 ISA 일반 상품과 신탁형·일임형 상품이 함께(유형 비교를 상품으로 나눴을 수 있음) |
| | `name_not_in_text` / `member_names_differ` | 모델 이름이 영역 문구에 없음(이름 null) / 묶은 대상들의 이름이 다름 |
| | `template_undetermined` / `type_unsupported` | 템플릿 미확정 / 지원 범위 밖 유형 |
| | `integration_failed` / `integration_missing_target` | 통합 요청 실패(대상을 잇지 못함) / 통합 응답에 빠진 대상 |
| | `candidate_pages` | 미검증 후보 경계(CANDIDATE_RUN) 페이지의 영역을 포함 |
| | `pension_exposure_undetermined` + `pension_pages_incomplete` / `pension_unassigned_ocr_terms` | 미노출 확정 불가: 미처리·관측 실패·판독 실패 페이지 / 영역 밖 OCR 에 펀드·ETF·ELB 등 용어 |
| | `pension_other_investment_only` | 국채·ELS·RP 만 실제 소개(포함, FUND/ETF/ELB 없음) |

## 4. 요청·검증 원칙

- 모델 응답은 ID·분류값·라벨·최소 근거(특징 ID·영역·인용)만 받는다. analysis·reason·요약은 받지 않는다.
- 인용 검사(`classify.quote_where`): NFKC·공백 제거·따옴표/가운뎃점/줄표 표기만 맞춘 뒤 인용 전체가 그 영역 최종 text, 기존 표 셀, 또는 표 한 행에 들어 있어야 한다. 부분 겹침·중간 생략 인용은 인정하지 않는다. 다른 영역 문구·스키마 예시 문구는 그 영역에 없으면 버려진다.
- 대상 검사: 근거 인용(유효)도 영역 문구 속 이름도 없는 대상은 상품으로 등록하지 않는다(trace `dropped_targets`).
- 모델 판단을 규칙으로 바꾸지 않는다. 결정 신호(모집인 표현, 퇴직연금 운용상품 용어)는 검토 코드로만 쓴다. 퇴직연금 포함/미노출은 묶음의 운용상품 근거(유효 인용)와 페이지 처리 상태로 정한다.
- 재시도·다수결 없음. 실패는 failed·코드로 남긴다.
- 미배정 OCR 은 모델에 보내지 않는다. 퇴직연금 미노출 확정을 막는 검토 단서로만 쓴다.

## 5. 실험 기록(지시 버전 변경 이력)

모든 실험 입력은 저장된 파싱 결과다(OCR·전사 호출 없음). 아래 입력은 모두 라벨 스키마 예시에 쓰인 광고(개발·회귀 자료, 7절)이므로
여기의 개선은 새 광고에 대한 정확도가 아니다. 이전 실행 폴더는 지우지 않고 기록으로 둔다(`work/semantic/`).

| 실험 | 목적(실행 전) | 바꾼 것 | 입력·폴더 | 결과 |
|---|---|---|---|---|
| E0 | 기준 동작 확인 | observe/1·label/1 | isa01 → `isa-general-2026-10-27-s1` | 상품 1(ISA 일반, 신탁형·일임형·상품 A/B·예금·펀드는 상품 아님). 문제: 이름 '…(ISA) 소개', 대상 하나뿐인데 설명 영역 19개가 shared, 가입유형 표(R22)에 '비과세한도'·'의무가입 기간' 인용으로 유의사항·수수료 라벨 |
| E1 | 위 세 문제가 일반 지시로 줄어드는지 | observe/2(상품명 부분만, 대상의 내용은 specific), label/2(표 항목명·조건값만으로 라벨 금지, 인용은 내용 자체, bbox 제거) | isa01 → `-s2` | 이름 '개인종합자산관리계좌(ISA)', specific 20·shared 7, R22 라벨 없음. 3회 호출·21.9초 |
| E1 확대 | 다른 템플릿에서도 유지되는지 | 같음 | docs11 9묶음 → `docs11-regression-s1` | 회귀 발견: g02(같은 ISA 광고의 이전 파싱)에서 비교표의 신탁형·일임형을 별도 상품 3개로, g01·g03 에 근거 없는 대상, label/2 의 조건 예시 문장 때문에 h01(지수연동예금)·g08 예금 상품 라벨 전부 빈 값, g06 라벨 응답의 같은 라벨·같은 영역 반복(퇴화)으로 label_missing 16 |
| E2a | 위 회귀를 일반 지시·구조로 고치는지 | observe/3: 영역 배정을 영역 별칭 키 객체로(반복·누락을 디코더가 막음), 회사·비교 유형은 대상 아님, 범위 밖 대상도 근거(other.product), '근거 없는 대상은 만들지 않는다' | isa01 → `-s3` | 실패: targets=[] 인데 영역은 t1 을 가리킴(상품 0) |
| E2b | E2a 실패 원인 문장 제거 | observe/4: 그 문장 삭제(코드 검사가 근거 없는 대상을 버림), 비교 지시 긍정형, 묶음 전체 관측은 대상 ≥1. label/3: 조건 예시 삭제·빠짐없이 지시 복원·영역 키 객체·영역당 라벨 ≤8 | isa01 `-s4`, docs11 `docs11-regression-s2` | g02 상품 1, g03 근거 없는 대상 없음, h01·g08 예금 라벨 회복, label_missing 0. 남은 문제: g06 페이지 관측 2쪽이 대상을 선언하지 않고 t1 을 가리킴, g01 대출 제한 고지를 범위 밖 대상으로 |
| E2c | 페이지 관측의 대상 미선언 | observe/5: 페이지 관측도 대상 ≥1(근거 없으면 검사에서 버림) | g06 → `docs11-regression-s3-g06`; 전체 → `final-2026-10-27` | 4쪽 모두 대상 선언. 전체: docs11·ISA·새 입력은 참고 기대와 파일 기준 모두 일치, **tmp 퇴직연금 광고 5개가 한 상품으로 묶이고 운용상품이 ELB 만**(ETF 라인업 쪽이 pension.common 만 인용), 합성 탐색 6/8(국채·ELS·RP 만·마지막 쪽 ETF 를 미노출로) |
| E3 | 퇴직연금 운용상품·제도 근거 누락과 별도 광고 묶임 | observe/6: 퇴직연금 대상은 제도·실제 소개 운용상품·고지 언급을 근거로 빠짐없이. integrate/2: 별도 광고는 파일별로, 대상별 제목 후보(짧은 전용 영역 문구, 자르지 않음). 파일마다 운용상품 소개 여부가 다르면 `pension_exposure_differs_by_file` | 전체 → `final-2026-10-27-e3` | 합성 탐색 8/8, tmp 퇴직연금 운용상품 FUND·ETF·ELB. 그러나 tmp 광고 5개는 여전히 한 상품, 안내장 라인업의 GIC·ELB·정기예금이 별도 대상, ISA 일반 쪽이 다시 유형별로 나뉨 |
| E4 | 지시 충돌('이름·조건 갖춘 소개 상품은 대상' vs '퇴직연금 운용상품은 근거') | observe/7: 계좌·제도 광고 안의 편입·운용 상품은 이름·금리가 있어도 대상 아님 | tmp·합성 → `e4-2026-10-27` | 합성 탐색 8/8. tmp 통합 응답이 T1 하나만 쓰고 28개를 빠뜨림(퇴화) → 상품 29 |
| E5 | 통합 응답의 대상 누락 | integrate/3: 대상 번호 키 객체(대상 → 상품 번호) + 상품 번호별 템플릿 | tmp·g06 → `e5-2026-10-27` | 누락 없음. 대신 과소 묶음: g06 한 상품의 탭 4개를 상품 4개로(같은 이름 2개 포함), tmp 안내장 8개로 |
| E6 | 과소 묶음 | integrate/4: E5 구조 유지, '별도 광고면 파일별로' 문장 삭제, 탭·이어지는 화면 설명 구체화 | g06·tmp → `e6-2026-10-27` | g06 한 상품. tmp 9/13 파일 일치(t06 미노출, t08·t09 포함·운용상품 일치). 안내장 t07 은 여전히 쪽별로 나뉘고 라인업 상품이 대상이 됨 |

고정 버전(최종): **observe/7 · integrate/4 · label/3 · classification-policy/1 · 라벨 스키마 358baf3**. 최종 실행은 `work/semantic/final-2026-10-27-v2/`. 지시를 더 고치지 않고 남은 오류는 8절에 그대로 적는다(같은 개발 자료로 계속 고치면 그 자료에 맞추는 것이 되기 때문).

같은 요청 반복: isa01 에서 프롬프트·스키마·이미지가 바이트 단위로 같은 관측 요청(s4 와 최종)의 응답이 달랐다(temperature 0 이어도 서버 응답이 고정되지 않음). 상품·템플릿은 같았고 차이는 경계 영역 2개(R20 '※ ISA계좌는 … 1개의 계좌만' 유의사항 여부, R04 '금' shared/unknown)다. 결과 재현은 다시 호출하는 대신 저장 응답 재구성(`--rebuild`)으로 한다.

## 6. 최종 결과(고정 버전, `work/semantic/final-2026-10-27-v2/`)

### 6.1 실행·호출·시간(GPU 동시 상한 2, 측정 최대 동시 2)

| 실행 | 묶음 | 쪽 | 영역 | 관측 | 통합 | 라벨 | 벽시계 |
|---|---:|---:|---:|---:|---:|---:|---:|
| isa-general(최신 ISA) | 1 | 1 | 28 | 1 | 0 | 2 | 21.5초 |
| docs11-regression | 9 | 19 | 283 | 12 | 1 | 22 | 207.8초 |
| new-inputs | 2 | 2 | 48 | 2 | 0 | 4 | 40.7초 |
| tmp-inputs(9개 광고를 한 묶음으로 올린 것) | 1 | 18 | 388 | 18 | 1 | 32 | 256.3초 |
| per-ad-t05~t09(퇴직연금 광고를 한 개씩) | 5 | 13 | 296 | 12 | 1 | 18 | 221초 |
| probe-cases(합성) | 8 | 10 | 44 | 8 | 0 | 6 | 48.1초 |

호출 140회(관측 53·통합 3·라벨 84), 실패 0. 작은 묶음은 관측 1 + 라벨 1~3회로 20~40초. 영역 28개 ISA 는 3회·21.5초(같은 페이지 파싱은 26회·245.7초).

### 6.2 상품·템플릿(참고 기대값과 파일 단위 비교, 개발·회귀 자료)

참고 기대값은 nh-ad-compliance 예시 README 의 광고별 '정답 템플릿'(평가 전용, 모델 입력 아님)이다.

| 사례 | 결과 | 비고 |
|---|---|---|
| 최신 ISA(isa01) | ISA 일반 상품 1개 '개인종합자산관리계좌(ISA)'. 신탁형·일임형 비교·상품 A/B 예시·ISA 안 예금·펀드는 상품 아님(holding 언급) | 일치 |
| 대출 포스터(f01) | 대출성상품-상품명 노출 'NH더퍼스트기업대출' + 이름 없는 대출 상품 1개 더(`template_evidence_missing`) | 불일치(추가 상품, 검토 표시) |
| ISA 일반(f02)·신탁형(f03) | 각 1개 | 일치 |
| 같은 상품 여러 파일: 카카오페이통장 3파일(f04~f06) | 한 product_id, `files` 3개 | 일치 |
| 긴 이미지 여러 타일·파일: 청년미래적금 4파일(f08~f11, 쪽별 관측 + 통합) | **정정(2026-10-27, docs/15 §1)**: 최종 실행에서는 상품 3개(f08+f11 / f09 / f10, 통합 응답 sem-0022). 한 product_id 였던 것은 E6 실행이다 | 불일치(파일별 템플릿은 모두 적립식이라 아래 파일 기준 집계에는 일치로 셌다) |
| 예·적금 한 페이지(f13)·두 페이지(f12) | 예금(거치식)·적금(적립식) 상품 2개, 회사명·심의필 영역은 shared 로 두 상품에 각각 라벨 | 일치 |
| 지수연동예금 HWP(h01, ELD) | 예금성상품-지수연동예금 | 일치 |
| 새 입력 n01(긴 PNG, 후보 경계)·n02 | 입출식(`candidate_pages`)·적립식 | 일치 |
| tmp 묶음(ISA 4 + 퇴직연금 5 광고) | 상품 30개: ISA 일반 쪽이 유형별 3개로(`isa_types_split`), 8쪽 안내장(t07)이 쪽마다 상품으로, 라인업 상품(GIC·ELB·정기예금)이 별도 상품 | 불일치. 같은 버전 직전 실행(E6)은 상품 21개로 결과가 크게 달랐다 |
| 퇴직연금 광고별(per-ad) | 연금-01·04 미노출 일치. 연금-05 퇴직연금 상품은 포함·FUND/ETF/ELB 일치하나 라인업 상품 3개 추가. **연금-06 미노출(기대 포함)**. 연금-07 포함·FUND/ETF 일치 + 미노출 퇴직연금 상품 1개 추가 | 5개 중 2개 완전 일치 |

파일 기준 일치 22/31(tmp·per-ad 포함). docs11·ISA·새 입력(한 광고씩 올린 묶음) 17개 파일 중 16개 일치.
이 집계는 파일마다 그 파일에 걸친 상품의 템플릿만 비교해 **파일 사이 상품 동일성(같은 상품이 여러 파일에 이어지는지)을 보지 않았다.**
묶음 단위 상품 수·연결을 함께 보면 18묶음 중 13개 일치다(g01·g06·t07·t09·tmp 불일치, docs/15 §1·§5).

### 6.3 라벨(최신 ISA, 스키마·원문 대조)

| 영역 | 라벨 | 판단 |
|---|---|---|
| 머리글 'NH Bank 금융상품몰 농협은행 농·축협' | 회사명 | 스키마 확정 예시와 같은 문구 |
| 고객투자 유의사항 묶음(7개 문장 + 준법감시인심의번호) | 유의사항·수수료·심의번호(한 영역 다중 라벨) | 유의사항·심의번호는 확정 예시와 일치. 수수료는 '이 계좌는 신탁보수 또는 일임수수료가 발생할 수 있습니다' 인용(아래 9절 확인 필요) |
| 가입대상 표, 가입유형별 제출서류 표, 비교표, 계산 예시, 메뉴·바닥글 | 없음(빈 label_ids, 완료) | ISA 일반 허용 라벨 4개에 해당하지 않음 |
| '금'(아이콘 글자) | 소속 미확정 → 라벨 미실행 | 검토 표시 |

여러 라벨 영역: 묶음별 0~11개(h01 11, g07 10, n02 8). 라벨 상태: 완료(라벨 있음)·완료(라벨 없음)·일부·미실행·실패를 구분해 남겼고 최종 실행에서 라벨 호출 실패는 0이다.

### 6.4 퇴직연금·혼동 사례

| 사례 | 실제 자료 | 결과 |
|---|---|---|
| 일반 고지에만 '(펀드)'·범주명 | 연금-01·04(per-ad), 합성 | 미노출(일치) |
| 펀드와 ETF 함께 | 연금-05·07(per-ad) | 포함 FUND+ETF(+ELB) 일치 |
| 실제 소개가 있는데 놓침 | 연금-06(per-ad) | **미노출로 오판**(운용상품 근거를 관측이 내지 않음) |
| ETF 만 | 합성만 | 포함·ETF |
| 마지막 쪽에만 ETF | 합성만 | 최종 실행 미노출(오판). E3·E4 실행에서는 포함·ETF |
| 국채·ELS·RP 만 | 합성만 | 포함·하위 유형 없음·`pension_other_investment_only` |
| DB 회사명(DB손해보험) | 합성만 | 퇴직연금 아님(unsupported) |
| 연금저축 | 합성만 | 퇴직연금 아님(unsupported) |
| ELD | h01(실제)·합성 | 지수연동예금 |
| 독립 ELB | 합성만 | 최종 실행 지수연동예금으로 오판(E2c~E4 실행에서는 unsupported) |
| ISA 안 ELB·ELS 언급 | f03·t02(실제) | 상품으로 만들지 않음(holding) |

합성 사례는 계약·분기 검사와 모델 반응 확인이며 실제 광고 품질 평가가 아니다. 계약·분기 검사는 `tests/test_semantic.py`(마지막 쪽 ETF·고지만·국채/ELS/RP·FUND+ETF 순서·판독 실패 쪽·영역 밖 OCR 용어·파일별 노출 차이).

## 7. 개발·회귀 자료 구분

`report.json` 의 `dev_regression_overlap` 은 라벨 스키마의 확정 예시 문구가 입력 영역 문구에 3개 이상 들어 있는 출처 광고를 적는다. 이번 실제 입력은 대출 포스터(f01)를 빼고 모두 스키마 예시 출처 광고다(ISA일반-01, 신탁-03, 일임-01·02, 연금-01·04·05·07, 입출-01·02·03·05, 적립-01·02, 혼합14-04·05, 지수-03 등). 대출성 상품명 노출은 원본에 예시가 없지만 분류 정책 예시 상품명에 'NH더퍼스트기업대출'이 있다. 따라서 위 일치 수는 새 광고에 대한 정확도가 아니다.

## 8. 남은 오류와 미검증 범위

- **같은 요청의 응답이 고정되지 않는다**: temperature 0 이어도 바이트가 같은 요청의 응답이 달랐다(docs11 14개 중 3개만 같음, tmp 19개 중 0개). 작은 묶음은 상품·템플릿 수준이 대체로 같았고(9묶음 중 8) 영역 배정·라벨은 파일마다 0~13개 영역이 달라졌다. 재현은 저장 응답 재구성으로만 보장된다.
- **큰 이질 묶음(쪽별 관측 + 통합)이 불안정하다**: 같은 버전으로 tmp 를 두 번 돌리면 상품 21개 vs 30개. 통합 지시를 바꾸면 과다 묶음(별도 광고 5개를 한 상품, integrate/1)과 과소 묶음(한 상품 탭 4개를 4개로, integrate/3) 사이를 오갔다. 여러 쪽 안내장(t07)은 쪽마다 상품이 되는 경우가 많다.
- **퇴직연금 운용상품 근거 누락·과다**: 연금-06 미노출 오판, 안내장 라인업의 GIC·ELB·정기예금을 별도 상품으로 만드는 경우.
- **추가 상품**: 대출 포스터의 이름 없는 두 번째 대출 상품, ISA 일반 쪽의 유형별 상품(같은 파일이면 `isa_types_split` 로 표시).
- **라벨 판단 경계**: 위 ISA 수수료·※ 문구, 혼합 영역의 상품별 인용 누락(`mixed_parts_missing`).
- **미검증**: HWPX 입력, 실제 자료가 없는 유형(대출모집인, 카드·보험 등 범위 밖, 퇴직연금 ETF 만·국채/ELS/RP 만·DB 회사명·연금저축의 실제 광고), 새 광고(스키마 예시에 없는 광고)의 정확도, 의미 단계를 포함한 `run_pipeline.py` 처음부터의 전체 실행(파싱은 다시 하지 않았다. 의미 단계 연결은 복사한 ISA 실행으로 확인).

## 9. 검사

| 검사 | 결과 |
|---|---|
| 오프라인 테스트 | `pytest` 116개 통과(기존 76 + 의미 단계 40: 자산·라벨 지식·인용 검사·분류 요청 중립성·단일/쪽별 흐름·통합 실패·관측 실패·라벨 실패·미지원 상품·퇴직연금 분기·템플릿 검토 코드·이름 검사·근거 없는 대상·계약 위반 9종·보존·재구성·동시성) |
| 계약·참조·보존·인용 위치 | 최종 실행 전체 통과(per-ad 실행은 한 파일만 떼어 돌린 평가라 묶음 입력 순서 검사 1건씩 의도된 불일치) |
| 파싱 값 보존 | 영역 수·순서·ID·bbox·text·text_status·표·파싱 검토·후보 경계·미배정 OCR·페이지 값 차이 0(이미지 경로는 같은 파일인지 확인) |
| 저장 응답 재구성 | 모든 최종 실행을 호출 0회로 다시 만들어 파일별 결과가 바이트 단위로 같음(`report.rebuild.json`) |
| 파일명 중립성 | 최신 ISA 의 파일명을 중립 이름·대출·퇴직연금 이름으로, file_id·submission_id 를 바꿔도 분류 요청 텍스트·스키마·이미지 해시가 같음. 기록된 프롬프트 120개에서 파일명·ID·manifest 설명·예시 출처 광고 이름 0건 |
| 동시성 | 모든 실행 최대 동시 호출 2(상한 2) |
| run_pipeline 연결 | 복사한 ISA 실행에서 의미 단계 실행(3회), 다시 부르면 덮어쓰지 않고 건너뜀, `--rebuild` 가 파싱·의미 단계를 호출 없이 재구성 |
