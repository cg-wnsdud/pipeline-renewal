# 06. 공유 OCR 서버의 클래스별 레이아웃 옵션: 적용 상태와 패치 계약(2026-10-25)

이 문서는 서버 담당자가 패치를 적용하고 검증할 때 쓰는 계약이다. 이 작업에서는 서버 설정·코드·컨테이너를 수정하거나 재시작하지 않았다. 읽기 전용 요청만 보냈다.

## 1. 현재 상태

### 원인(서버 조사, 2026-10-25, 확인됨)

- **증상**:
  - 클래스별 `layoutThreshold`를 dict로 보내면 모든 클래스에 0.5가 적용된다.
  - 클래스별 `layoutMergeBboxesMode`를 dict로 보내면 서버 YAML의 병합 설정 전체가 '필터 없음(union)'으로 바뀐다.
- **원인**:
  - 요청 JSON의 dict 키는 문자열이다(`"2"`). 그런데 후처리(`DetPostProcess`, `check_containment`)는 정수 키로 조회하거나 비교한다.
  - 요청 스키마는 키를 검증하지 않는다. 그래서 `{"abc":0.7}`도 200으로 수락된다.
  - 요청 dict는 YAML 값을 병합하지 않고 통째로 대체한다.
- **제안된 수정**: 서빙 래퍼(`/app/app/server.py`)에서 `pipeline.predict`를 감싼다.
  - dict 키를 정수로 바꾸고, YAML 클래스별 값 위에 요청 키만 덮어쓴다.
  - 잘못된 키와 값은 422로 거절한다.

### 적용 여부 확인

- **실행**: `scripts/probe_layout_options.py`로 읽기 전용 요청 8회를 보냈다. 결과는 `work/server-probe/2026-10-25/probe.json`이다.
- **입력**: h01_p03(같은 이미지).
- **판정: 패치 미적용.**

| 요청 | HTTP | 상자 수 | 해석 |
|---|---:|---:|---|
| 기본(YAML) | 200 | 13 | 기준 |
| `layoutThreshold: 0.5`(숫자) | 200 | 10 | 점수 0.5 미만 `paragraph_title` 3개가 빠짐 |
| `layoutThreshold: {"2": 0.4}`(텍스트 클래스만, YAML과 같은 값 의도) | 200 | 10 | **숫자 0.5와 상자까지 같음** → 키 무시, 전체 0.5 |
| `layoutThreshold: {"abc": 0.7}` | 200 | 10 | 잘못된 키가 수락됨(패치 후 422 기대) |
| `layoutThreshold: {"2": 1.5}` | 200 | 10 | 잘못된 값이 수락됨(패치 후 422 기대) |
| `layoutMergeBboxesMode: "union"` | 200 | 13 | 이 페이지에서는 기본과 같음 |
| `layoutMergeBboxesMode: {"0": "large"}` | 200 | 13 | 기본·union과 같음. **이 페이지로는 병합 dict를 판별할 수 없다** |
| `layoutMergeBboxesMode: "large"`(통합 경로의 현재 설정) | 200 | 11 | 문자열 값이라 버그 대상이 아님 |

**현재 운영 경로에는 영향이 없다.** 통합 경로는 `layoutThreshold`를 보내지 않고, `layoutMergeBboxesMode`는 문자열 `"large"`로 보낸다. dict 버그는 클래스별 실험을 할 때만 문제가 된다.

## 2. 패치 계약(서버 담당자 적용 → 이 저장소가 검증)

| 항목 | 요구 |
|---|---|
| 적용 범위 | `layoutThreshold`, `layoutMergeBboxesMode`(dict일 때). `layoutUnclipRatio` dict는 조사하지 않았으므로 별도 확인 대상 |
| 키 | `"0"`~`"19"` 문자열을 정수 클래스 ID로 변환. 숫자가 아니거나 범위를 벗어나거나 라벨 이름(`"text"`)이면 422 |
| 값 | threshold는 0~1 숫자(bool 제외). merge mode는 `union`/`large`/`small`. 그 밖의 값이면 422 |
| 병합 | YAML의 클래스별 값을 기본으로 두고 요청 키만 덮어씀. 요청에 없는 클래스는 YAML 값 유지 |
| 숫자·문자열 | 기존 동작 그대로(숫자 threshold는 전 클래스, 문자열 mode는 전 클래스) |
| 빈 dict | 두 옵션 모두 YAML과 같아야 함. 현재는 threshold의 빈 dict가 전체 0.5로 떨어진다 |
| 기록 | 응답 `model_settings` 또는 로그에 적용된 클래스별 값을 남기면 좋음(선택) |

## 3. 검증 항목

### 1차: probe 재실행

`scripts/probe_layout_options.py --page-image <h01_p03> --out work/server-probe/<날짜>`를 다시 실행한다. 판정이 `patch_applied`여야 한다.

- `{"2":0.4}` 결과가 기본 결과와 같다(13상자, 상자 목록 동일).
- `{"abc":0.7}`과 `{"2":1.5}`가 4xx다.
- 숫자 0.5는 10상자로, 지금과 같다.

### 2차: 병합 dict

h01_p03은 union과 기본이 같아서 판별하지 못한다. 기본과 union이 다른 이미지로 다시 확인해야 한다. 서버 조사에 쓴 690×976 이미지에서는 기본 27, union 30이었다.

- `{"0":"large","1":"large"}`(YAML과 같은 의도)가 기본(27)과 같아야 한다.

### 3차: 회귀

통합 경로의 현재 옵션(문자열 `"large"`)으로 Stage 1을 다시 실행한다. 그 결과가 `work/runs/stage1-merge-large`와 영역 수·bbox에서 같아야 한다.

## 4. 패치 검증 뒤 할 실험(지금은 막혀 있음)

**목적**: 웹 레이아웃 누락을 줄인다. 정답 줄 중 미배정 OCR에만 남은 줄이 다수다(docs/03). 방법은 텍스트류 클래스(`text`, `paragraph_title` 등)의 문턱값만 낮추고, 표·이미지 클래스는 YAML 값을 유지하는 것이다.

**비교 조건**: Stage 1 전체 manifest에 대해 다음 두 조건을 비교한다.

- 기준: 문자열 `"large"`, threshold 생략.
- 비교: 기준 + 텍스트류 threshold dict.

**평가**(모두 정답 대조):

- 정답 줄 home 비율
- 미배정 OCR에만 있는 줄 수
- 영역 겹침 수
- 표 후보 수

**호출**: Stage 1 OCR 약 20회 × 조건 수.
