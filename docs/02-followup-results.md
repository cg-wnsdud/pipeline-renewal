# 02. 후속 결과 — 지표 분리 · 배정 감사 · 검출 옵션 · VLM 판독 · 경계 계약

기준일: 2026-10-24. 지시: `pipeline-redesign-2026-10-24/claude-next-stage-prompt.md`.

- 참조 자료는 모두 열람할 수 있었다: `stage1-review-and-next-steps.md`, `boundary-vlm-findings.md`, `design.md`, 독립 감사 JSON, `boundary-vlm/`, `boundary-single/`.
- 서버 버전은 Codex의 `actual-input-design/server-profile-2026-10-24.json`(사용자 제공 기록, 미검증)으로 확인했다.
- 모델 호출: OCR 27회(확인 1 + 5페이지×2조건 10 + 16페이지 확인 16), VLM 77회(일반 14 + 표 v1/v2/v3 각 20 + 스키마 진단 2 + 스키마 지원 확인 1).
- 서버 설정은 바꾸지 않았다.

## 0. 단계별 상태(일반 16페이지 / 긴 3페이지 분리)

| 단계 | 상태 | 비고 |
|---|---|---|
| 실행 | 일반 16페이지 READY. 긴 f08은 candidate 경계(실행 안 함), f09·f11은 BOUNDARY_PENDING | READY는 실행 완료일 뿐 품질 통과가 아니다 |
| 보존 계약 | 통과 | 원시 layout 200 = Region 200, 원시 OCR 1159 = 배정 762(빈 문자열 2) + 미배정 397(빈 문자열 11) |
| 영역 검출(고정 기하) | 미통과 | 정답 1103줄: 단일 749 / 복수 0 / 일부 197 / 미포함 157 |
| 일반 영역 원문 품질 | 제한 실험(29영역)만 측정 | B(영역 crop): 정답 줄 160/165, CER 1.5%. 전 영역 판독은 미실행 |
| 표 후보 | 제한 실험(20개) | is_table 19/19 일치(+1 판독 실패), f13 조건-값 10/10 |
| 미검증 | — | 긴 입력 최종 OCR, 일반 영역 전량 판독, 동시성, 300DPI, HWPX 실제 샘플, 상품 연결 |

## 1. 지표 분리와 배정 감사(저장 응답, 호출 없음)

**지표를 둘로 나눴다.** 이전 보고의 78.6%는 배정 규칙(`classify`)을 정답 줄에 적용한 단일 배정률이었고, 검출 재현율로 부르면 안 된다.

1. **영역 검출(고정 기하 기준 `exact-containment/1`)**: 정답 줄 bbox가 원시 bbox에 허용 오차 없이 들어가는 영역 수로 미포함/일부/단일/복수를 정한다.
   - 배정 규칙과 무관하고 모든 설정 비교에서 고정한다.
   - '일부'는 포함 면적비 구간과 함께 보고한다.
2. **배정 정책**: 단일 배정률.
3. **원시 OCR 보존**: 배정+미배정의 정확한 분할.
4. **VLM 원문 품질**: 별도 평가(4절).

**완화 규칙(core 0.25) 감사**: 같은 응답에 엄격 포함과 0.25 규칙을 적용했다.

- 엄격 기준에서는 미배정인데 0.25 규칙이 배정한 항목이 177개였다(일부 포함 175 + 여백 겹침 2).
- 177개 전부를 '돌출한 쪽 경계를 확대한 시트'로 시각 검토했다(Claude, 사람 검토 아님).

| 판정 | 수 | 의미 |
|---|---:|---|
| glyph_outside | 39 | 영역선이 글자 획을 지나감. 주로 하단 2~12px(%p·Deposit의 p 꼬리 등) |
| unclear | 30 | 배경 그래픽·배너·로고·버튼 테두리로 판단 불가 |
| padding_only | 108 | 획은 영역 안, OCR 상자 여백만 밖 |

- 지시에 따라 **기본을 엄격 포함으로 되돌렸다**. 0.25는 `--core-shrink 0.25` 비교용으로만 남긴다.
- 원래 OCR bbox가 겹치지 않는 경우(`outside_layout`)와 여백만 걸친 경우(`margin_overlap_only`)를 구분했다.
- 부수 관찰: PP-DocLayout 상자는 글자 하단을 자주 몇 px 자른다(과소 bbox). 이 때문에 정답 줄 '일부'가 197개다(면적 95% 이상이 안에 있는 줄이 83개).
- 근거: `work/audit/assignment-base/assignment-audit.json`, `relaxed-sheet-1..5.png`, `relaxed-review.json`.

## 2. 검출 옵션 비교(대표 5페이지 → 우수 후보만 16페이지)

**옵션의 의미는 PaddleX v3.6.1 소스로 확인했다.** 확인한 파일: `PP-StructureV3.yaml`, `object_detection/processors.py`, `predictor.py`, `serving/schemas/pp_structurev3.py`, `_pipeline_apps/pp_structurev3.py`.

- `layoutThreshold`: 실수 또는 dict. dict는 서버 설정 dict를 통째로 대체하고, 없는 클래스는 0.5다.
- 순서: 클래스별 문턱값 → NMS(같은 클래스 IoU 0.6, 다른 클래스 0.98) → `layoutMergeBboxesMode`.
  - 포함의 정의: 상자 면적의 90% 이상이 다른 상자 안.
  - `large`: 포함된 상자를 제거한다.
  - `small`: 다른 상자를 포함하는 바깥 상자를 제거한다.
  - 서버 기본은 paragraph_title·image·formula·chart만 large이고 나머지는 union이다.
- **확인 호출 결과, class별 threshold dict는 의도대로 적용되지 않았다.** (2026-10-25 정정: 원인은 미확정)
  - h01_p03에 기본값과 같은 dict를 보내자 13상자가 10상자가 됐다. 점수 0.5 미만 제목 3개가 빠진 것으로, 전 클래스 0.5처럼 동작한 것이다.
  - 공개 소스의 int 키 조회와 JSON 문자열 키 불일치 가설과 부합한다. 다만 실행 중인 컨테이너 코드는 확인하지 않았으므로 원인 확정이 아니다.
  - 서버 확인 전까지 class별 threshold와 class별 병합 모드는 비교 조건으로 쓰지 않는다.

**5페이지 비교**: f01, f05, f12_p02, f13, h01_p03. 정답 기하는 단일/복수/일부/미포함 순이다.

| 조건 | f01 | f05 | f12_p02 | f13 | h01_p03 | 판단 |
|---|---|---|---|---|---|---|
| 서버 기본 | 53/3/29/0 | 23/0/3/45 | 61/4/37/0 | 66/3/36/0 | 29/2/5/0 | 기준 |
| threshold 0.3(기존 실행) | 53/5/27/0 | 58/2/5/6 | 54/16/32/0 | 65/6/34/0 | 5/31/0/0 | 웹 누락은 줄지만 중첩 폭증. 기각 |
| merge `small` | 42/0/42/1 | 23/0/3/45 | 45/0/42/15 | 51/0/43/11 | 26/0/3/7 | 미포함 증가. 기각 |
| merge `large` | 56/0/29/0 | 23/0/3/45 | 65/0/37/0 | 69/0/36/0 | 31/0/5/0 | 중첩 0, 손실 없음. 16페이지 확인 |

- **16페이지 확인(`large`)**:
  - 정답 기하: 미포함 157 동일, 일부 197 동일, 단일 735→749, 복수 14→0.
  - 표 후보 20/20 유지, 상품 혼합 영역 0, 큰 영역 수 동일.
  - 원본 비교 이미지로 f01·f13·f12_p02·f07_p01을 확인했다. 바깥 상자가 남고 안쪽 상자가 빠지며, 좌우 상품은 섞이지 않았다.
- **잠정 채택**: `config/layout-options.json`. 서버가 반환한 원시 결과이며 클라이언트 병합이 아니다.
  - 위험: 큰 저점수 컨테이너가 생기면 안쪽 영역이 모두 사라진다. `REGION_COVERS_LARGE_AREA` 이슈로 표시한다.
- **남은 문제(모델 한계로 기록)**: 둘 다 클라이언트 병합으로 고치지 않았다.
  - 웹 캡처 레이아웃 누락 157줄: f05 45, f04 31, f06 26, f02 24, f03 22, h01_p01 7.
  - 과소 bbox로 인한 '일부' 197줄.
- 근거: `work/compare/detect-5p/*.png`, `detect-16p-large/`, `work/runs/verify-threshold-dict`, `cmp5-merge-*`, `stage1-merge-large`.

## 3. 현재 기준 실행: `work/runs/stage1-merge-large`

엄격 배정 + merge large 조건이다.

- **영역**: 200, 표 후보 20.
- **정답 줄**: 기하 단일 749, 배정 정책 단일 배정 748/1103(67.8%).
- **원시 OCR**: 글자 있는 1146개 중 배정 760, 미배정 386(밖 185, 일부 200, 모호 1).
- **f08**: Codex 실험 근거로 만든 `boundary-plan/2` candidate 문서를 표시만 한다.
  - cut 1668 unknown, 3336 unknown, 4903 unsafe(제목-설명 분리).
  - OCR은 실행하지 않았다.
- **대조 화면**: `work/runs/stage1-merge-large/viewer/index.html`.

## 4. VLM 판독 실험(동시 1, 결과를 보기 전에 고정한 평가 목록)

**평가 목록(`fixtures/vlm-eval-set.json`)**: 일반 영역 29개와 표 후보 20개 전부.

- 일반 영역은 f01·f05·f13·h01의 크기·배치가 다른 영역, f12_p01 손글씨, f12_p02 좌우 비표 항목-값 목록이다. 손글씨는 f12_p02가 아니라 f12_p01에 있다.
- 표 라벨은 VLM 실행 전에 제가 시각 검토로 정했다: 표 18(경계 사례 4: 탭이 있는 웹 패널), 표 아님 2(f07 상단 메뉴).
- 정답 소속 기준(`eval-membership/1`): 정답 줄 면적의 50% 이상이 영역 안이면 기대 줄, 일부만 걸치면 채점 제외.
- 혼입은 출력 줄 단위로 판정한다. 처음 문자열 부분 일치로 셌을 때 줄바꿈된 상품명이 오탐으로 잡혀 고쳤다.

### 4.1 일반 영역: A(원래 페이지 배치) vs B(검출 영역 전체 crop)

같은 Stage 1 영역과 같은 OCR 참고를 썼다.

- A: 원본 페이지 + 대상 영역을 표시한 별도 사본 + ID/bbox/초안. 페이지당 HTTP 1회.
- B: 원본 그대로 자른 영역 crop 여러 장(요청당 최대 8장) + 이미지별 ID.

| 방식 | HTTP | 계약 통과 | 정답 줄 찾음 | 정확 일치 | 문자 오류율 | 혼입 | 숫자 누락/추가 | 시간 | 토큰(입력/출력) |
|---|---:|---:|---|---:|---:|---:|---|---:|---|
| A 페이지 배치 | 7 | 29/29 | 154/165 | 138 | 13.7% | 0 | 19/1 | 70초 | 24.4k/3.4k |
| B 영역 crop | 7 | 29/29 | 160/165 | 152 | 1.5% | 0 | 3/0 | 109초 | 40.2k/3.8k |

- **서버 리사이즈 추정**: A의 페이지는 f13 0.42배, f01·h01 0.82배로 축소된다. B의 crop은 2~20배로 확대된다.
- **A의 실패**: f01 L005·L007은 영역을 다시 읽지 않고 **배정된 OCR 초안 한 줄만 옮겼다**. 엄격 배정이라 나머지 줄은 '경계 OCR'로만 전달됐다. f13·f01 하단에서도 줄 누락이 있었다.
- **B의 실패**:
  - 영역 경계에서 1~3px 잘린 글자를 놓쳤다: f12_p01 손글씨 "사랑합니다! 응원합니다!"(A는 읽음), f12_p02 '가입대상' 2건.
  - 로고 문구 'NH농협금융'(f13 L002)을 생략했다.
  - f01 L026의 한 줄에 오류가 있었다.
- **잠정 판단**: 일반 영역은 **B가 우세**하다. 다만 경계 잘림과 로고 생략이라는 실패 유형이 남아 최종 확정하지 않는다.
  - 다음 후보 실험: 저장 bbox는 그대로 두고 crop에만 작은 여백을 넣는 방식. 이웃 문구 혼입을 함께 측정해야 한다.
- **검토 표시 정확도**:
  - `needs_review`(품질+맥락 근거)는 두 방식 모두 실패 영역 6/6을 표시했다. 그러나 정답 영역 23개 중 20개도 표시했다. 엄격 배정에서 흔한 '경계에 걸친 OCR' 맥락 근거 때문이다.
  - 품질 근거만 보면 B 실패 6개 중 2개만 잡혔다. OCR도 놓친 손글씨나 경계 잘림 글자는 OCR과 비교할 근거가 없기 때문이다.
  - 즉 현재 검토 신호는 보수적이지만 변별력이 낮다. `uncertain=false`는 실패 영역에서도 그대로였다.
- **Region 간 반복 줄**: f13 6줄, f12_p02 1줄. 모두 원시 중첩이 아닌 별개 영역(좌우 상품)에 실제로 같은 문구가 있는 경우다. merge large 이후 중첩으로 인한 중복은 0이다. 측정만 했고 삭제하지 않았다.

### 4.2 OCR 표 후보(20개, crop 1장, is_table·전체 문구·행 대응을 한 번에)

| 요청 버전 | 계약 | is_table 라벨 일치 | 정답 줄 | CER | 행(rows) | f13 조건-값 |
|---|---|---|---|---:|---|---|
| v1: `rows=[{cells}]`, text 뒤 | 20/20 | 20/20 | 307/346 | 12.4% | 18개 표 전부 빈 배열 | 0/10 |
| v2: 같은 형태, rows를 text 앞에 | 20/20 | 20/20 | 343/346 | 0.36% | 18개 표 전부 빈 배열 | 0/10 |
| 진단 2회(f13 L000) | — | — | — | — | `rows: string[][]`면 강제 없이 5행 생성 | — |
| **v3: `rows: string[][]`** | 19/20 | 19/19(+1 실패) | 331/346 | 1.5% | 17개 생성, 불완전 4개 표시 | **10/10** |

- **거짓 표**: f07 상단 메뉴 2개는 is_table=false로 판정됐고, 같은 bbox에 문구가 보존됐다.
- **v3 실패 1건(f01 L006)**: 출력이 밑줄 반복으로 퇴화해 `finish_reason=length`가 됐다. 계약 검사가 `read_failed`로 잡았고, 재시도로 숨기지 않았다.
- **v3 내용 오염 1건(f12_p02 L016)**: JSON 스키마를 통과했지만 `text`에 모델 내부 토큰(`<|file_separator|>`)이 섞였다. `special_token_in_text`로 표시한다.
- **행 누락**: f02 L002, f04 L003, f06 L003, f12_p02 L016의 rows가 text를 85% 미만으로 덮어 `table_rows_incomplete`로 표시했다. 행 대응을 정답과 대조한 것은 f13 두 표뿐이다.
- **선택**: 표 요청은 v3를 쓴다. 하나의 실패 형태를 진단한 뒤 스키마를 바꾼 것이며, 반복 재시도가 아니다.
- **비후보 처리**: 비후보 항목-값 목록(f13 정보, f12_p02 정보)은 표 후보 누락으로 기록하고 일반 판독으로 평가했다. 표 전용 호출은 추가하지 않았다.

**대조 화면**: `work/vlm/eval-r1/viewer/index.html`(A/B, v1 표), `work/vlm/eval-r1-table-v3/viewer/index.html`(v3 표). 원시 응답은 `calls/`, 보낸 이미지는 `inputs/`에 있다.

## 5. 긴 이미지 경계 계약 강화(오프라인)

- **필수 항목**: 실행할 계획(`boundary-plan/2`)은 다음이 모두 있어야 한다. 하나라도 어긋나면 `BOUNDARY_PLAN_INVALID`(실행 안 함).
  - 해시·크기·좌표계·계획 버전
  - 정책 이름·버전·한도(검증 대상 값임을 표시)
  - 내부 cut별 근거, 승인된 의미 검증 방식(`human_review`)과 결과 `safe`
- **실행 전 검사**: 전체 경계열이 0부터 전체 길이까지 겹침 없이 이어지고 모든 타일이 한도 안에 있어야 한다.
- **VLM 판정은 candidate로만 저장한다.** 다수결, `uncertain=false`, 문자열 `validated`로는 실행되지 않는다.
- **`compose_sequence`**: 검증된 안전 위치만으로 한도 안의 연결된 경계열을 구성한다(경계별로 골라 이어 붙이지 않음). 실패하면 `no_safe_boundary` 또는 `oversized_region`이다.
- **품질 상태**: 실행 상태와 품질 상태를 분리했다. 타일 경계에 닿은 영역은 품질 `review_required`가 된다.
- **오프라인 테스트**: 필드 누락 9종, cut 오류 5종, 경계열 구성 3종 등 35개 통과.

## 6. 남은 일과 필요한 정보

1. **일반 영역 판독**: B 경계 잘림·로고 생략에 대한 여백 crop 1개 조건을 비교한다. 그다음 일반 16페이지 전 영역을 판독해 페이지 단위로 정답 대조하고, 동시성 1/2/4를 비교한다.
2. **검토 표시 변별력**: 맥락 근거와 품질 근거를 분리해 보고하는 것은 이미 했다. 남은 일은 OCR도 놓친 글자(손글씨 등)를 잡는 근거가 없다는 점이다. 모델 한계로 기록하고 사람 검토 대상에 둔다.
3. **긴 입력**: 사람 검토 방식으로 검증된 경계 문서가 들어오면 바로 실행된다. 자동 의미 경계는 미통과 상태로 유지한다.
4. **서버 정보(필요 시)**: class별 threshold로 웹 누락(157줄)을 줄이는 실험에는 서버 쪽에서 dict 키를 정수로 바꿔 주는 처리가 필요하다(서빙 앱 수정 또는 파이프라인 설정 파일의 threshold 변경). 이것 없이는 "표·이미지는 그대로, 텍스트류만 낮춤" 조건을 시험할 수 없다. 나머지 작업은 이 정보 없이 계속할 수 있다.
5. **미검증**: 300DPI(아직 해상도로 특정된 오류 없음), HWPX 실제 광고 샘플, 상품 연결·라벨링.

## 재현

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/audit_assignment.py work/runs/stage1-base --out work/audit/assignment-base
.venv/Scripts/python.exe scripts/run_stage1.py --rebuild work/runs/stage1-base --dest work/runs/stage1-base-r2
.venv/Scripts/python.exe scripts/compare_runs.py --run base=work/runs/stage1-base-r2 --run thr0.3=work/runs/stage1-cmp-layout03-r2 \
  --run merge-large=work/runs/cmp5-merge-large --run merge-small=work/runs/cmp5-merge-small \
  --pages f01_p01,f05_p01,f12_p02,f13_p01,h01_p03 --out work/compare/detect-5p
.venv/Scripts/python.exe scripts/run_stage1.py --manifest fixtures/manifest.json --manifest fixtures/manifest-hwp.json --out work/runs/stage1-merge-large
.venv/Scripts/python.exe scripts/run_stage1.py --rebuild work/runs/stage1-merge-large --boundary-plans fixtures/boundary-plans
.venv/Scripts/python.exe scripts/run_vlm_eval.py --run work/runs/stage1-merge-large --set fixtures/vlm-eval-set.json --out work/vlm/eval-r1
.venv/Scripts/python.exe scripts/run_vlm_eval.py ... --out work/vlm/eval-r1-table-v3 --modes table
.venv/Scripts/python.exe scripts/run_vlm_eval.py ... --out work/vlm/eval-r1 --reeval   # 저장 응답 재평가(호출 없음)
```
