# layout-pipeline

입력에 보이는 문구를 확보하고 원본 위치(OCR 레이아웃 검출 bbox)와 연결하는 파서다.

- `../new-pipeline`은 비교 근거로 보존하며, 이 폴더는 그 코드를 import하지 않는다.
- 기존 코드에서 옮긴 것은 HWP/HWPX→PDF 변환 연결(`hwp_bridge.py`) 하나다.

## 현재 범위와 상태(2026-10-27)

**실행 경로**: 입력 → 페이지 준비 → OCR(일반 페이지는 전체 1회) → 레이아웃 영역/배정/미배정 → 모든 Region VLM 판독 → 최종 JSON·trace·대조 화면.

**기본 프로필(page-first-default/1, docs/12)**: `run_pipeline.py` 기본값은 다음과 같다.
- 판독: page-first, 필요한 영역만 원본 crop으로 재판독(reread).
- 표: table-cells/1.
- 배정: strict(VLM 입력의 OCR 근거).
- 미배정 출력(unassigned-output/1): 결과의 `unassigned_texts`는 어느 영역에도 OCR 상자 면적 50% 이상 들어가지 않은 줄만 둔다. 영역 안 글자는 영역 문구 쪽으로 본다(docs/12 §7).
- 긴 이미지: 승인 경계(해시가 같은 페이지만) → 없으면 자동 경계 → 정책 범위 밖이면 후보 경계로 실행(CANDIDATE_RUN 표시).
- 실행 옵션은 `--manifest`와 `--out`만 지정한다(VLM 동시 2).

**선택 배정 규칙 `overlap-best/1`**(비교용, 기본 아님, VLM 입력과 재판독 조건이 바뀜): `--assignment-rule overlap-best`를 주면 같은 OCR view의
레이아웃 영역과 양의 면적으로 겹치는 OCR 줄을 가장 많이 겹친 한 영역에 귀속한다. 겹침이 같은 경우
더 작은 영역을 고른다. 겹침이 없는 줄만 `outside_layout` 미배정으로 남는다(잘못된 면적 0 bbox는
`invalid_bbox` 예외). 50% 미만 겹침과 여러 영역 경합은 검토 사유로 기록하고 page-first에서는
해당 영역을 재판독한다. 50% 미만 겹침 OCR은 영역 기록에는 배정하지만 VLM의 확정 OCR 초안으로
전달하지 않는다. 현재 strict 기본값은 유지하며, 저장된 OCR 응답으로 먼저 비교할 수 있다.

```bash
.venv/Scripts/python.exe scripts/run_stage1.py --rebuild <기존-stage1> --dest <새-stage1> --assignment-rule overlap-best
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest <manifest.json> --assignment-rule overlap-best --out <새-출력폴더>
```

이전 crop 판독은 `--read-mode crop`, 이전 표 판독은 `--table-read legacy`로 비교할 수 있다.

**최종 결과**는 입력 파일별 `layout-result/2`다(docs/12).
- `<out>/files/<file_id>/result.json`: 여러 페이지 PDF/HWP는 모든 페이지를 한 문서에 담는다.
- `<out>/files/index.json`: 파일 목록.
- `<out>/files/index.html`: 파일·페이지를 고르는 대조 화면.

영역마다 상품 배정·라벨링 필드가 있고, 이번 단계에서는 `not_run`이다. 상품 배정과 라벨링은 아직 구현·실행하지 않았다. 실행 단위 `<out>/result.json`(layout-result/1)은 내부·재구성용이다.

아래 항목은 경로별 설명이다(이전 기본값 기준 서술 포함).

- **일반 영역**: 검출 영역 전체 crop을 묶음 요청으로 판독한다. 기본 지시는 `crop/1`이다.
  - 판독 원칙 v2·v3(`crop/2`, `crop/3`, 이미지 기준 읽기 순서 + 정렬된 OCR 후보)는 선택지로만 있다.
  - 비교 결과 사전 기준에 미달했다(docs/04). 채택은 사용자 판단 대상이다.
- **표 후보**: `table-check/3`로 확인한다. rows가 있는데 text가 비면 rows로 채우고, `text_from_table_rows`로 표시한다.
- **검토 근거**: 품질 근거와 맥락 근거를 나눈다.
  - 품질: OCR↔VLM 어절 단위 불일치, 숫자 불일치, 응답 잔여물 등.
  - 맥락: 영역 겹침, 경계 걸침, 겹친 영역의 중복 문구 등.
  - 문구·bbox는 고치지 않는다.
- **페이지 우선 판독**(`--read-mode page-first`, docs/08·09):
  - **판독**: 원본 페이지(또는 타일)와 영역 표시 사본을 한 번에 보내 region_id별 문구와 표 후보 판정을 받는다.
  - **영역 확인**(`--verify-mode reread`, docs/10): 일반 조건에 걸린 영역만 한 영역씩 다시 읽는다. 원본 영역 crop과 1차 판독 단위(페이지·띠·타일) 문맥 이미지를 함께 보내 완결된 문구 후보를 받고, 고정 정책으로 1차와 재판독 중 하나를 고른다. 두 후보는 모두 trace에 남는다. 표는 text만 재판독하고, 표 결과가 없거나 불완전할 때만 표 crop을 다시 읽는다. 이전 방식(region-patch/1)은 `--verify-mode patch`로 재현할 수 있다.
  - **띠 나누기**: 서버 축소가 0.7배 미만인 페이지는 VLM 입력만 영역 사이에서 띠로 나눈다(page-band/1). OCR과 영역은 그대로다.
  - **상태**: 기본값은 crop이다. 최신 결과는 `work/pipeline/page-first-reread-2026-10-26`(재판독)이고, 이전 결과는 `page-first-v2-2026-10-26`(보완 방식)이다. docs/11 변경(표 셀 구조·순서 확인 조건)을 더한 19페이지 회귀는 `page-first-docs11-regression-2026-10-26`, 새 입력 2개 최종 실험은 `new-inputs-page-first-final-2026-10-26`·`new-inputs-crop-final-2026-10-26`(ink-containment/1 배정)이다.
- **표 구조 판독**(`--table-read cells`, docs/11): 표 후보마다 원본 표 crop + 문맥 이미지로 text 와 논리 셀(row·col·rowspan·colspan·text)을 한 응답으로 받아 하나의 후보로 쓴다(table-cells/1). crop·page-first 두 모드가 같은 경로를 쓴다. rows 는 셀에서 변환한 호환 보기다. 기본(`legacy`)은 이전 경로이며, 재판독이 표 text 만 바꾸고 rows 가 1차 값으로 남으면 `table_text_rows_from_different_reads` 품질 검토를 단다.
- **순서 확인 조건**(simple-column-order/1, docs/11): 한 단으로 쌓인 영역에서 문구 줄 순서가 위→아래와 어긋나면 page-first 는 재판독으로 보내고, 최종 문구에 남으면 `reading_order_conflict` 품질 검토를 단다. 문구를 OCR 순서로 고치지 않는다.
- **배정 규칙**: 기본은 엄격 포함(strict). `ink-containment/1`(OCR 상자 여백만 영역 밖인 경우만 배정)은 `scripts/run_stage1.py --rebuild --assignment-rule ink-containment` 로 고를 수 있는 비교 규칙이다(docs/11 §1).
- **긴 이미지**: 확정 경계 문서(`boundary-plan/2`, `validated`)가 있으면 타일 OCR·판독을 한다. 없으면 보류하고, 사유를 결과와 화면에 표시한다.
  - **f08·f09·f11**: 사용자 승인 경계(human_review, 2026-10-26)로 확정했다(`fixtures/boundary-plans`).
  - **자동 경계 정책**(`--auto-boundaries`, `config/boundary-policy.json`, docs/09):
    - 절차: 탐색 OCR → 빈 띠 → 비율 2.5 이하로 구성 → VLM 관계 판정 → 금지 관계(제목–본문, 같은 문장, 표 행 사이)면 빼고 재구성.
    - 상태: 사용자 승인과 3페이지 검증(passed)을 마쳤다. 범위(폭 640~700px, 비율 ≥ 3.0) 안의 새 예시는 자동으로 확정하고, 범위 밖은 후보로 처리한다.
  - **입력 크기**: 페이지 전체나 더 큰 창은 레이아웃 검출이 나빠 허용하지 않는다(docs/05). f09 [0,2131] 한 타일에서는 각주 영역이 빠졌다.
  - **후보 실험 실행**: `--run-candidate-boundaries`로 실행한다. 상태는 `CANDIDATE_RUN`이며 확정 결과가 아니다.
- **서버 클래스별 옵션(dict)**: 패치가 **아직 적용되지 않았다**(읽기 전용 확인, docs/06). 현재 경로는 문자열 `"large"`만 보내므로 영향이 없다.
- **품질**: 정답이 있는 16페이지만 평가했다(문구 보존·구조 보존·OCR 대비·검토 표시). 원문·위치·표 관계 품질은 통과로 표시하지 않는다.
- **범위 밖**: 상품 연결·템플릿 선택·라벨링은 후속 단계다. HWPX 실제 광고 샘플은 제공되지 않았다.

문서:
- [docs/00-correction.md](docs/00-correction.md): 계약
- [docs/02-followup-results.md](docs/02-followup-results.md): 지표 분리·검출 옵션·VLM 1차
- [docs/03-pipeline-results.md](docs/03-pipeline-results.md): 평가 보완·여백 crop·통합 실행·경계 실험·동시성
- [docs/04-reading-principles-plan.md](docs/04-reading-principles-plan.md): 판독 원칙 v2·v3 비교(사전 계획 → 결과 → 판정)
- [docs/05-long-input-plan.md](docs/05-long-input-plan.md): 긴 이미지 입력 크기 비교
- [docs/06-server-layout-options.md](docs/06-server-layout-options.md): 서버 클래스별 옵션 적용 상태와 패치 계약
- [docs/07-results.md](docs/07-results.md): 판독 원칙 비교 단계 종합 보고
- [docs/08-page-first-and-long-plan.md](docs/08-page-first-and-long-plan.md): 페이지 우선 판독·긴 이미지 후보 실행(계획 → 결과)
- [docs/09-boundary-policy-and-page-read-fixes.md](docs/09-boundary-policy-and-page-read-fixes.md): 경계 정책 확정·자동 경계 검증·페이지 우선 판독 수정(계획 → 결과)
- [docs/10-region-reread-plan.md](docs/10-region-reread-plan.md): 영역 재판독으로 영역 소속·읽기 구조 확인(계획 → 결과)
- [docs/11-assignment-table-structure-plan.md](docs/11-assignment-table-structure-plan.md): OCR 배정 원인 실험·표 셀 구조 판독·순서 확인 조건(계획 → 결과)
- [docs/12-file-result-contract.md](docs/12-file-result-contract.md): 입력 파일별 결과 계약 layout-result/2(스키마 `schemas/layout-result-2.schema.json`), 기존 결과 내보내기, tmp 입력 실행
- [docs/boundary-experiment-plan.md](docs/boundary-experiment-plan.md): 경계 실험 사전 계획

## 실행

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe "pillow>=11" "pypdfium2==5.12.0" "requests>=2.32" "pytest>=8" "numpy>=2" "jsonschema>=4.21"
cp .env.example .env

# docs/12: 기본 프로필로 입력부터 실행(파일별 결과 files/<file_id>/result.json 과 files/index.html 생성).
# 옵션은 입력과 출력 폴더만. 승인 경계·자동 경계·후보 경계 실행·VLM 동시 2·미배정 출력 규칙이 기본값이다.
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest work/manifests/tmp-inputs-2026-10-27.json --out work/pipeline/<새 폴더>
# 기존 실행을 파일별 결과로 내보내기 + 보존 검사(호출 없음, 원래 실행은 읽기만)
.venv/Scripts/python.exe scripts/export_file_results.py --run work/pipeline/page-first-docs11-regression-2026-10-26 --out work/export/<새 폴더>
# docs/11: 배정·참고 전달 감사(호출 없음), 작은 비교, 새 입력 정답 대조, 기존 실행에 새 호출만 더한 회귀
.venv/Scripts/python.exe scripts/audit_reference_flow.py --stage1 work/pipeline/new-inputs-stage1-2026-10-26/stage1 \
    --run work/pipeline/new-inputs-page-first-2026-10-26 --run work/pipeline/new-inputs-crop-2026-10-26 \
    --out work/audit/new-inputs-2026-10-26 --cases work/audit/new-inputs-2026-10-26/cases.json
.venv/Scripts/python.exe scripts/run_docs11_experiments.py --exp E3,E2,E1a,E1b --out work/docs11-exp --concurrency 1
.venv/Scripts/python.exe scripts/eval_new_inputs.py --run <out1> --run <out2> --out work/compare/x
.venv/Scripts/python.exe scripts/run_targeted_update.py --base work/pipeline/page-first-reread-2026-10-26 --out work/pipeline/x
# 영역 재판독 실험(기존 Stage 1·1차 응답 재사용, docs/10)
.venv/Scripts/python.exe scripts/run_reread_experiment.py --run work/pipeline/page-first-v2-2026-10-26 --set core --conditions A,B --reps 2 --out work/reread/exp1
# 최종(재판독): 1차 page 응답 30개를 새 폴더 trace/vlm 에 복사한 뒤 --resume 으로 확인 단계만 실행
.venv/Scripts/python.exe scripts/run_pipeline.py --from-stage1 work/pipeline/page-first-v2-2026-10-26/stage1 --read-mode page-first     --verify-mode reread --resume --vlm-concurrency 2 --out work/pipeline/page-first-reread-2026-10-26
# 페이지 우선 판독 v2 + 긴 이미지 확정 경계(승인) + 자동 경계(계획 없는 새 긴 이미지)
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/manifest.json --manifest fixtures/manifest-hwp.json \
    --boundary-plans fixtures/boundary-plans --auto-boundaries --reuse-ocr work/runs/stage1-merge-large \
    --read-mode page-first --vlm-concurrency 2 --out work/pipeline/page-first-v2-2026-10-26
# (이전) 페이지 우선 판독 + 긴 이미지 후보 경계 실험 실행(일반 페이지 OCR 은 기존 Stage 1 재사용, 새 타일만 OCR)
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/manifest.json --manifest fixtures/manifest-hwp.json \
    --boundary-plans fixtures/boundary-plans-candidate --run-candidate-boundaries --reuse-ocr work/runs/stage1-merge-large \
    --read-mode page-first --vlm-concurrency 2 --out work/pipeline/page-first-2026-10-26
.venv/Scripts/python.exe scripts/eval_pipeline.py work/pipeline/page-first-2026-10-26
# 통합 실행(입력 파일부터). 출력 폴더는 비어 있어야 함. manifest 의 파일 경로·순서대로 처리
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/manifest.json --manifest fixtures/manifest-hwp.json \
    --boundary-plans fixtures/boundary-plans --vlm-concurrency 2 --out work/pipeline/final-2026-10-25
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/examples/normal-page.json --out work/examples/normal
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/examples/bundle.json --out work/examples/bundle
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/examples/two-products.json --out work/examples/two-products
.venv/Scripts/python.exe scripts/run_pipeline.py --manifest fixtures/examples/long.json --boundary-plans fixtures/boundary-plans --out work/examples/long
# 정답이 있는 입력의 평가(사용자 결과와 분리된 <out>/eval/)
.venv/Scripts/python.exe scripts/eval_pipeline.py work/examples/normal
# 저장된 원시 응답으로 결과·화면 재생성(호출 없음)
.venv/Scripts/python.exe scripts/run_pipeline.py --rebuild --out work/pipeline/full-c1
# 여러 실행의 구조·관계·OCR 추종·검토 표시 비교(구조 정답 fixtures/structure-gold.json, 사례 fixtures/vlm-cases.json)
.venv/Scripts/python.exe scripts/eval_structure.py --out work/compare/x --baseline c1,c2 c1=work/pipeline/full-c1 c2=work/pipeline/full-c2 new=work/pipeline/final-2026-10-25
# 판독 원칙 선택지(기본 crop/1·table-check/3, docs/04)
.venv/Scripts/python.exe scripts/run_pipeline.py --from-stage1 work/runs/stage1-merge-large --general-request crop/3 --table-request table-check/3 --out work/pipeline/x

# Stage 1만(OCR·영역·배정), 실험 도구
.venv/Scripts/python.exe scripts/probe_layout_options.py --page-image work/runs/stage1-merge-large/pages/h01_p03.png --out work/server-probe/x
.venv/Scripts/python.exe scripts/run_long_input_experiment.py eval --out work/long/exp2
.venv/Scripts/python.exe scripts/run_stage1.py --manifest fixtures/manifest.json --out work/runs/x
.venv/Scripts/python.exe scripts/run_vlm_eval.py --run work/runs/stage1-merge-large --set fixtures/vlm-eval-set.json --out work/vlm/x
.venv/Scripts/python.exe scripts/run_boundary_experiment.py scout --out work/boundary/x
.venv/Scripts/python.exe scripts/compare_concurrency.py
.venv/Scripts/python.exe -m pytest -q
```

## 통합 실행 출력(`<out>/`)

| 파일 | 내용 |
|---|---|
| `result.json` | **사용자 결과(layout-result/1)**. 담는 항목은 아래 목록 참고 |
| `trace/vlm/*.json` | VLM 요청 메타(프롬프트·스키마·이미지 크기/해시·crop 범위)와 원시 응답 |
| `trace/reads.json` | Region별 판독 근거(호출 ID, 계약 오류, 검토 측정값) |
| `trace/timing.json` | 단계별 시간·동시성 |
| `stage1/` | OCR 원시 응답·영역·배정 기록·Stage 1 화면 |
| `viewer/index.html` | 최종 대조 화면. `#페이지ID/영역ID`로 바로 이동. 기본은 영역별 최종 결과(VLM 최종 문구·판독 실패·품질 검토·맥락 검토). 'OCR 참고'·'평가용 정답 대조' 레이어는 꺼진 상태로 시작하며, 출처 라벨로 구분한다 |
| `eval/` | (평가를 실행한 경우) 페이지·영역·표 평가 |

`result.json`에 담기는 항목:
- 페이지: 좌표계 `page_px_xyxy`, 실행 상태, 품질 상태
- Region: 원본 bbox, VLM 최종 문구, `text_status`(read / no_text / read_failed / not_read), 표 확인(table / not_table / unknown)과 `rows`, 검토 근거(품질/맥락 구분)
- 판독 실패 영역: OCR 참고(`final=false`)
- 미배정 OCR(최종 문구와 합치지 않음), 이슈, 계약 검사
