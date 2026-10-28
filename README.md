# layout-pipeline

금융 광고 입력(이미지·PDF·HWP)을 페이지별 영역과 최종 문구로 파싱하고, 광고 상품·템플릿·영역 소속·라벨까지 정해 파일별 JSON 과 확인 화면을 만드는 파이프라인.

## 파일 구조

```
layout_pipeline/            파이프라인 코드
  pages.py, hwp_bridge.py     1 입력 → 페이지(PDF 200dpi 렌더, HWP/HWPX 는 PDF 변환)
  routing.py, auto_boundary.py, boundary.py
                              긴 이미지 판정·자동 경계(타일 나누기)
  stage1.py, ocr_client.py, regions.py, assignment.py, diagnostics.py, ink.py
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

출력: `<out>/files/<file_id>/result.json`(파싱), `<out>/semantic/files/<file_id>/result.json`(최종), `<out>/semantic/report.html`(입력 검토 화면), `<out>/trace/`(판단 근거·원시 응답·시간).

## 단계별 로직

1. **입력·페이지 준비**: 한 번의 업로드를 하나의 광고 묶음으로 처리한다. PDF 는 200dpi 이미지로, HWP/HWPX 는 기존 변환 경로(hwp_bridge)로 PDF 를 만든 뒤 같은 방식으로 페이지를 만든다.
2. **OCR 영역·글자 검출**: 일반 페이지는 전체를 한 번에 OCR 서버(PP-StructureV3)에 보낸다. 세로가 가로의 3배 이상인 긴 이미지는 승인된 경계 문서가 있으면 그 경계로, 없으면 자동 경계(영역 사이 빈 띠 중 제목–본문을 가르지 않는 곳, section-pack/2)로 타일을 나눠 보낸다. 자동 경계 정책의 검증 범위(폭 640~700px) 밖이면 후보 경계로 실행하고 `candidate_pages` 검토를 붙인다. 레이아웃 상자가 영역(`…-L000`)이 되고 OCR 글자는 영역에 배정하거나 미배정으로 남긴다.
3. **VLM 영역별 최종 문구 판독**: 페이지(긴 페이지는 띠·타일) 이미지에 영역 상자를 그려 보여 주고 영역마다 최종 문구를 받는다. OCR 은 참고이고 최종 문구는 VLM 판독 결과다. 누락·순서 어긋남·상자 밖 문구 등 조건에 걸린 영역만 원본 crop 으로 재판독하며, 재판독이 다른 영역 글자를 가져오면 채택하지 않는다. 표 후보는 문구와 행·열 셀 구조를 함께 받는다.
4. **놓친 영역 보충**: 판독 뒤 어느 영역에도 들지 않은 OCR 글자를 같은 줄·위아래로 붙은 것끼리 묶고(2행×2열 이상이면 표 후보), 그런 글자가 있는 페이지는 표 문턱을 0.3 으로 낮춘 OCR 을 한 번 더 받아 표 상자를 찾는다. 새 영역은 원본에서 다시 읽고, 기존 영역과 겹치거나 문구가 이웃 영역에 이미 있으면 버린다. 추가 영역은 `…-S000` ID 와 `supplement_region` 검토로 구분하고 근거는 `trace/supplement.json` 에 남긴다.
5. **상품·템플릿·영역 소속 결정(observe)**: 영역 상자 이미지와 전체 영역의 파싱 결과(문구·표)를 VLM 에 보내 상품이 몇 개인지, 상품별 템플릿, 모든 영역이 어느 상품에 속하는지(한 상품·여러 상품 공통·미확정)를 한 호출에서 함께 정한다. 응답의 상품 번호·영역 연결을 검사하고, 이름도 자기 영역도 없는 대상은 버리고(R1) 이름 없는 대상이 같은 템플릿 대상과 겹치면 합친다(R2). 큰 묶음(4쪽·80영역·16,000자·이미지 6장 초과)은 페이지별로 확인한 뒤 통합 호출로 파일·페이지에 이어지는 같은 상품을 잇는다. 요청에는 파일명·파일 ID 를 넣지 않는다.
6. **영역별 라벨링**: 영역에 연결된 상품의 템플릿 라벨 스키마에서 해당 구분값을 고른다. 여러 상품의 공통 영역은 상품별로 라벨링하고, 소속이 불명확한 영역은 미확정으로 남긴다. 라벨 근거 인용은 영역 문구에서 확인한다.
7. **최종 JSON·확인 화면**: 파일마다 `layout-result/3`(페이지, 영역 bbox·최종 문구·표 결과, 상품·템플릿, 영역의 상품 연결·라벨, 검토 상태)을 쓰고 스키마·계약 검사를 한다. 상세 판단 근거·프롬프트·원시 응답은 `trace/` 에 따로 두며, 저장 응답만으로 같은 결과를 다시 만들 수 있다.
