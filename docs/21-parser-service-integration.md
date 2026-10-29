# 21. nh-ad-compliance 파서 교체 준비: 등록 템플릿·미노출 템플릿·예금자보호 표시·review-input/2·서비스 실행(2026-10-29)

nh-ad-compliance 의 parser-pipeline(nh-parser-fin)을 이 파이프라인으로 바꾸기 위해 파이프라인 쪽에서 해야 할 일을 했다.
연결 방식(사용자 결정): 심의 입력은 우리 구조(review-input)로 두고 nh-ad-compliance 쪽이 맞춘다(2026-10-28). 판정에 넘기는 표는
지금 표 상태(판독 문구 + 셀 구조) 그대로다(2026-10-29). 등록 화면의 템플릿 선택은 선택 사항이 된다(2026-10-29, 고르지 않으면 자동 배정).

## 1. 등록 템플릿 요청(template-request/1)

manifest 묶음에 `template_request: {"template_ids": [...]}` 를 둘 수 있다(서비스 요청도 같은 이름). 의미 단계가 이 요청을 받는다.

| 등록 화면 선택 | mode | 동작 |
|---|---|---|
| 없음 | auto | 지금처럼 지원 템플릿 전체에서 분류 |
| 1개 | fixed | 관측·통합 요청의 템플릿 기준과 응답 스키마 후보를 그 템플릿으로 줄이고, 상품 확정에서 모든 상품의 템플릿을 그 하나로 둔다(기존 parser-pipeline `--template-id` 와 같음). 모델 판단과 다르면 상품 검토 코드 `template_from_request`. 결정 신호 필터(decisive-signal/1)는 적용하지 않는다(사용자 지정) |
| 여러 개 | restricted | 관측·통합의 템플릿 기준·스키마 후보를 그 템플릿들로 줄인다. 상품마다 그 안에서 고르고, 어느 것에도 맞지 않으면 undetermined. 결정 신호가 없을 때의 대체 템플릿이 등록 목록 밖이면 빼지 않고 `decisive_signal_missing` 만 남긴다 |

- 프롬프트에 `[등록 템플릿]` 안내 한 단락을 더한다(auto 면 요청 글이 이전과 같다).
- 모르는 템플릿(이 파이프라인이 분류하지 않는 것, 예: 카드)이면 OCR·모델 호출 전에 멈춘다(서비스는 입력 오류).
- 결과: layout-result/3 `semantic.template_request {template_ids, mode}`(묶음 단위), trace `template_request`.

## 2. 상품명 미노출 템플릿(classification-policy/4)

nh-ad-compliance 운영 등록 화면은 판정 카탈로그 16개 중 대출·예금·투자 12개만 보여 준다(카드 4종 제외, 펀드·ETF·ELB 는 퇴직연금
운용상품으로만). 이전 정책 10개에 없던 둘을 더했다.

- `loan_unnamed`(대출성상품-상품명 미노출, loan-unnamed), `deposit_unnamed`(예금성상품-상품명 미노출, deposit-unnamed).
  라벨 스키마는 nh-ad-compliance 의 것을 그대로 가져왔다(import_semantic_assets.py, 예시 문구 없음 = 템플릿 예시문구로만 라벨링).
- 정책 규칙: 대출·예금 광고 대상의 상품명이 광고 문구에 없으면(name=null) 미노출 템플릿(기존 parser-pipeline 도 상품명
  미노출이면 이 템플릿). `special_codes.unsupported` 에서 '상품명 미노출 대출·예금 광고' 를 뺐다.
- 대출모집인 결정 신호가 없어 대출모집인을 뺄 때 대체 템플릿은 상품명이 있으면 상품명 노출, 없으면 상품명 미노출이다.
- 상품명 유무와 템플릿이 어긋나면 검토 코드만 남긴다(모델 판단을 바꾸지 않음): `unnamed_template_with_name`, `named_template_without_name`.
- 검증 범위: 전량 테스트(whole-test)에 미노출 광고 정답이 없어 실제 광고로는 확인하지 못했다(단위 테스트만).

## 3. 예금자보호 표시(deposit-marks/1)

기존 parser-pipeline(dev 186ddca `parse/deposit_marks.py`)의 방식과 프롬프트를 옮겼다(`layout_pipeline/deposit_marks.py`).

- 후보: 영역 최종 문구나 그 영역에 배정된 OCR 글자(stage1 기록)에 `예금보험공사`·`보호금융상품`·`비보호` 가 있는 영역(메뉴 이름
  `보호금융상품등록부` 등은 뺌), 글자가 짧은 영역부터 12개.
- 확인: 영역 crop(여백 12px, bbox 밖은 흰색, 짧은 변 320px 로 최대 4배 확대, 1400px 이내)을 VLM 에 보내 보호·비보호마다
  YES/NO/UNSURE. 관측과 같은 동시 상한 안에서 관측과 함께 보낸다.
- 결과(layout-result/3 최상위 `deposit_insurance_marks`, 묶음 단위): 표시마다 `{status: PRESENT|ABSENT|UNKNOWN, code, region_ids}`.
  UNKNOWN code: CHECK_FAILED·MARK_UNCLEAR·TOO_MANY_CANDIDATES·PAGES_INCOMPLETE(처리 못 한 쪽·판독 실패 영역이 있음)·
  DISABLED(`LP_DEPOSIT_MARKS=off`)·NOT_CHECKED(이 기능 이전 실행의 재구성). 표시 안 글자(mark_text)와 후보별 응답은 trace 에만.
- 표시가 있어야 하는지는 판단하지 않는다(심의가 판단).

## 4. 심의 단계 입력 review-input/2

| 항목 | review-input/1 | review-input/2 |
|---|---|---|
| 검토 사유 | `review_reasons`(판독 품질 + 의미 단계) | `quality_reasons`(판독 품질) · `semantic_codes`(소속·라벨). 판정이 판독 가능 여부에 쓰는 것은 품질뿐 |
| 묶음 쪽 번호 | 없음(파일 안 `page_no` 만) | `pages[].seq`(입력 파일 순서 → 쪽 순서, 1부터) |
| 문구 출처 | 없음 | `text_source`: `hwp`(HWP 구조 영역 원문) · `vlm` · 문구가 없으면 null |
| 등록 템플릿 | 없음 | `template_request {template_ids, mode}` |
| 예금자보호 표시 | 없음 | `deposit_insurance_marks`(이 기능 이전 결과면 NOT_CHECKED) |
| HWP 검토 HTML | 없음 | `files[].hwp_review_html`(기존 변환기의 원문 재구성 HTML, 이 파일 기준 상대 경로) |
| 스키마 | 없음 | `schemas/review-input-2.schema.json`(handoff check 가 검사) |

검토 사유 분리 근거(whole-test 4,192영역): review-input/1 의 needs_review 28.6% 중 판독 품질 사유는 12.8%, 의미 단계 코드는 18.7%
(대부분 지원 범위 밖 상품 영역의 `product_not_classified`). 기존 parser-pipeline P3 의 needs_review 는 10.7%(6,044영역).
nh-ad-compliance 판정 엔진은 needs_review 영역을 '문구 결합·부재 확인 불가'로 다루므로 합친 값을 그대로 넘기면 판독은 정상인 영역까지 빠진다.

## 5. 서비스 실행(parser-service/1)과 원본 미리보기

- `scripts/run_service.py --request <요청 JSON> --out <실행 폴더>`: 요청 검사 → manifest(<실행 폴더>.manifest.json) → run_pipeline
  기본 프로필 그대로 실행 → `<실행 폴더>/service.json`. 형식·상태 판정은 `layout_pipeline/service.py`.
- service.json: `status`(completed|partial|failed), `failure{code, message}`, `review_input`(심의 입력 경로), `files`(layout-result/3 경로),
  `pages`(쪽 상태), `regions{total, read_failed}`, `checks{parse, semantic, review_input}`, `timing`.
- 종료 코드: 0 완료·부분 완료, 2 입력 오류(요청 형식·형식 미지원·HWP 변환 실패·모르는 템플릿), 3 처리 못 한 쪽·모든 영역 판독 실패·의미
  단계 실패(OCR·VLM 서버 문제일 수 있어 다시 시도 가능), 4 계약 검사 실패, 1 그 밖의 예외.
- 서비스 결과 ZIP = 실행 폴더 그대로(stage1·files·semantic·trace) + service.json. 경로가 모두 실행 폴더 안이라 복사·경로 재작성이 없다.
  크기는 whole-test 기준 6~23MB(대부분 쪽 이미지).
- 원본 미리보기 `layout_pipeline/preview.py`: PDF 는 파싱과 같은 함수(`pages.render_pdf_page`, 200dpi)로 그리고 PNG 인코딩도 같다
  (테스트에서 바이트 비교). HWP·HWPX 는 `python -m layout_pipeline.preview <원본> <폴더>` 로 변환(배경 레이어 재변환 포함) 뒤 같은
  방식으로 그린다. 기존 parser-pipeline 은 스캔 PDF 를 내장 해상도(72~200dpi)로 그렸으므로 교체 뒤에는 미리보기도 이 함수를 써야 한다.

## 6. Linux 실행

- 글꼴: 영역 번호(ASCII)를 그리는 글꼴을 `LP_FONT_PATH`(실제 환경 변수) → 맑은 고딕(Windows) → Noto CJK·DejaVu(Debian 경로) 순으로 찾는다.
  Windows 는 이전과 같은 그림이다.
- Python 3.11: `report_html.py` 의 3.12 전용 f-string 한 곳을 고쳤다. 전체 소스가 3.11 로 컴파일된다.
- 의존성: `pyproject.toml` + `uv.lock`(jsonschema, pillow, pypdfium2==5.12.0, requests; 개발 pytest). numpy 는 쓰지 않아 뺐다.
  pypdfium2 5.12.0 은 PyPI 에서 yanked 표시(휠은 5.12.1 과 같다는 공지)지만 기존 parser-pipeline 과 같은 픽셀을 위해 고정한다.
- HWP: 컨테이너에서는 document-processor 가 설치된 같은 인터프리터를 쓴다(`PN_HWP_PYTHON`=그 python, `PN_HWP_PROJECT`=파이프라인 폴더).
  ASCII 경로면 정션을 만들지 않는다.

## 7. 검증

- 단위 테스트 156개(모델 호출 없음): Python 3.12(.venv), Python 3.11(uv.lock 으로 만든 별도 환경) 모두 통과.
- 실제 OCR·VLM 서비스 실행(`work/service-e2e-2026-10-29`, `scripts/run_service.py`):

| 건 | 입력 | 등록 템플릿 | 소요 | 결과 |
|---|---|---|---:|---|
| E1 | whole-test w058 HWP 1개 | 없음(auto) | 181초 | completed. 거치식(정답과 같음), HWP 구조 영역 23개 전부 text_source=hwp, 검토 HTML 경로 확인, 보호 표시 후보 없음(ABSENT) |
| E2 | w060 PNG 3개 | 입출식(fixed) | 310초 | completed. 세 파일이 한 상품, 입출식(모델 판단과 같아 검토 코드 없음), 보호 표시 PRESENT(1개 영역, mark_text '예금보험공사 보호금융상품 1인당 최고 1억원') |
| E3 | ADR-0002 샘플 예금성 PNG | 없음(auto) | 175초 | completed. 적립식, 범위 밖 대상 1개, 표시 후보 없음 |
| E4 | w024 PNG 2개 | 상품명 노출 + 대출모집인(restricted) | 183초 | completed. 관측 스키마 후보가 두 템플릿 + 특수 코드로 줄어듦, 상품명 노출(정답과 같음) |

## 8. nh-ad-compliance 쪽(별도 브랜치에서 진행)

parser-pipeline 교체, 파서 서비스(app.py: run_service 실행·종료 코드 → HTTP 상태·templateId 반복·fileId·미리보기), Worker·판정 엔진·
백엔드의 새 형식 읽기(공용 모듈 `nh_ad_parser_contracts.review_input`: review-input/2 → 판정용 보기), 등록 화면 템플릿 선택 사항화,
Docker·compose, 문서·ADR.
