# 구글폼 예약 제출 매크로 (비로그인)

지정한 **구글 서버 시각**에 구글폼 응답이 도착하도록 제출하는 파이썬 매크로입니다.
브라우저나 구글 로그인 없이 `formResponse` 로 직접 POST 합니다.

## 설치

```bash
pip install -r requirements.txt
```

Python 3.9 이상이 필요합니다.

## 사용법

### 가장 간단한 방법: 문항 제목으로 답 적기

`config.example.json` 을 `config.json` 으로 복사한 뒤 폼 주소, 시각, 답만 바꾸면 됩니다.

```json
{
  "form_url": "https://forms.gle/xxxx",
  "target_time": "2026-10-01 10:00:00",
  "answers": {
    "학번": "2023142177",
    "성함": "김준석",
    "농구, 빙구 선택": "농구"
  }
}
```

- 키에는 문항 제목을 **정확히 쓰거나 일부만** 써도 됩니다(`"학번"` → `학번(ex 2025142154)`). 공백과 `*` 는 무시합니다.
- 객관식 값은 선택지 문구와 똑같아야 합니다. 틀리면 제출 전에 오류를 내고 멈춥니다.
- 날짜는 `"2026-10-01"`, 시간은 `"10:30"` 처럼 적습니다.
- 제목으로 찾으려면 `run` 을 시작할 때 폼이 열려 있어야 합니다. 목표 시각 전까지 폼이 닫혀 있다면,
  열려 있을 때 `inspect` 로 확인한 `entry.숫자` 키를 대신 쓰세요.

### 1. 문항과 entry ID 확인, 설정 파일 만들기

```bash
python gform_macro.py inspect "https://forms.gle/xxxx" -o config.json
```

문항별 `entry.숫자` 키, 선택지, 필수 여부를 출력하고 `config.json` 템플릿을 만듭니다.
`answers` 와 `target_time` 을 수정하세요.

| 문항 유형 | 값 적는 법 |
|---|---|
| 단답/장문/객관식/드롭다운/선형배율 | `"entry.123": "값"` (객관식은 선택지 문구와 정확히 같아야 함) |
| 체크박스 | `"entry.123": ["A", "B"]` |
| 날짜 | `entry.123_year`, `_month`, `_day` |
| 시간 | `entry.123_hour`, `_minute` |
| 객관식 그리드 | 행마다 entry ID가 따로 있음 |
| 기타(직접입력) | `"entry.123": "__other_option__"`, `"entry.123.other_option_response": "내용"` |
| 이메일 수집(직접 입력형) | `"emailAddress": "me@example.com"` |

폼이 아직 닫혀 있어서 문항을 볼 수 없다면, 폼 작성자가 공유한 **미리 채워진 링크**를
`"prefill_url"` 에 넣으면 거기 있는 `entry.*` 값을 답변으로 씁니다.

### 2. (선택) 서버 시간 오차 확인

```bash
python gform_macro.py sync "https://docs.google.com/forms/d/e/폼ID/viewform"
```

### 3. 예약 제출

```bash
python gform_macro.py run config.json            # 설정대로 실행
python gform_macro.py run config.json --dry-run  # 제출 없이 타이밍만 확인
python gform_macro.py run config.json --at "10:00:00" --offset-ms 30
```

프로그램을 켜 둔 채로 두면 목표 시각에 자동으로 제출합니다.

## 동작 방식

1. **서버 시간 측정**: 구글 서버의 HTTP `Date` 헤더는 초 단위라서 그대로 쓰면 최대 1초 오차가
   납니다. 그래서 요청이 서버의 **초가 바뀌는 순간**에 도착하도록 송신 시각을 조절하면서,
   응답한 초 값으로 추정 범위를 반씩 줄여 나갑니다(이분 탐색). 10번 정도면 보통 수 ms 이내로 수렴합니다.
2. **직전 재동기화**: 목표 시각 `resync_before_sec`(기본 40초) 전에 다시 측정해서, 오래 기다리는
   동안 생긴 PC 시계 드리프트를 없앱니다.
3. **연결 예열**: 1.5초 전에 keep-alive 연결을 데워 두고 요청을 미리 만들어 둡니다(TLS 핸드셰이크 지연 제거).
4. **정밀 송신**: `목표시각 − 서버오차 − RTT/2` 에 송신해서 요청이 목표 시각에 서버에 **도착**하게 합니다.
   마지막 20ms 는 busy-wait 으로 맞춥니다.
5. **결과 판별/재시도**: 폼이 아직 닫혀 있으면 `retry_interval_ms` 간격으로 `retry_until_open_sec` 동안
   다시 제출합니다. 성공, 입력 오류, 판별 불가일 때는 중복 제출을 막기 위해 바로 멈춥니다.

## 설정 항목

| 키 | 기본값 | 설명 |
|---|---|---|
| `form_url` | – | 폼 주소 (`forms.gle` 단축 주소도 가능) |
| `target_time` | – | 목표 시각. `2026-10-01 10:00:00`, `10:00:00`(오늘/내일), ISO 형식 |
| `timezone` | `Asia/Seoul` | `target_time` 의 시간대 |
| `answers` | – | 답변 |
| `send_offset_ms` | `0` | 도착 시각 보정. +면 늦게, −면 일찍 도착 |
| `retry_until_open_sec` | `10` | 닫혀 있을 때 재시도할 최대 시간 |
| `retry_interval_ms` | `300` | 재시도 간격 |
| `resync_before_sec` | `40` | 직전 재동기화 시점 |
| `sync_probes` | `10` | 동기화 탐색 횟수 |
| `probe_url` | `https://docs.google.com/favicon.ico` | 시간 측정용 주소 |
| `page_count` | 자동 | 섹션이 여러 개인 폼의 페이지 수(자동 감지가 안 될 때) |

## 제한 사항

- **로그인이 필요한 폼은 안 됩니다**: 응답 1회로 제한, 인증된 이메일 수집, 조직 내부 전용, 파일 업로드 문항이 있는 폼이 여기에 해당합니다.
- 폼이 닫혀 있는 동안에는 문항 정보를 읽을 수 없습니다. 미리 `inspect` 해 두거나 `prefill_url` 을 쓰세요.
- 정확도는 네트워크 상태(RTT, 지연 비대칭)에 따라 달라집니다. `sync` 에 나오는 `±` 값을 참고하세요.
- 폼 작성자가 정한 규칙과 이용 약관 범위 안에서 본인 응답을 제출하는 데만 쓰세요.
