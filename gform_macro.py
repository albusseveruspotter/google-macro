#!/usr/bin/env python3
"""구글폼 예약 제출 매크로 (비로그인).

구글 서버의 HTTP ``Date`` 헤더로 서버 시계를 밀리초 단위까지 추정한 뒤,
지정한 서버 시각에 응답이 도착하도록 ``formResponse`` 로 POST 합니다.

사용 예:
    python gform_macro.py inspect <폼 URL> -o config.json   # 문항/entry ID 확인 + 설정 템플릿 생성
    python gform_macro.py sync <폼 URL>                     # 서버 시간 오차 측정
    python gform_macro.py run config.json                   # 예약 제출
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DEFAULT_TZ = "Asia/Seoul"

# FB_PUBLIC_LOAD_DATA_ 의 문항 타입 코드
QUESTION_TYPES = {
    0: "단답형",
    1: "장문형",
    2: "객관식",
    3: "드롭다운",
    4: "체크박스",
    5: "선형 배율",
    6: "제목/설명",
    7: "객관식 그리드",
    8: "섹션(페이지 구분)",
    9: "날짜",
    10: "시간",
    11: "이미지",
    12: "동영상",
    13: "파일 업로드",
    18: "평점",
}


# ---------------------------------------------------------------------------
# 시계
# ---------------------------------------------------------------------------
class Clock:
    """perf_counter 기반 단조 시계. 측정 도중 OS 시계가 보정돼도 흔들리지 않는다."""

    def __init__(self) -> None:
        self.wall0 = time.time()
        self.perf0 = time.perf_counter()

    def now(self) -> float:
        return self.wall0 + (time.perf_counter() - self.perf0)

    def wait_until(self, t: float, on_tick=None) -> None:
        """로컬 시각 t 까지 대기. 마지막 20ms 는 busy-wait 으로 정밀하게 맞춘다."""
        last_tick = None
        while True:
            remain = t - self.now()
            if remain <= 0.02:
                break
            if on_tick is not None:
                sec = int(remain)
                if sec != last_tick:
                    last_tick = sec
                    on_tick(remain)
            time.sleep(min(remain - 0.02, 0.2))
        while self.now() < t:
            pass


# ---------------------------------------------------------------------------
# 서버 시간 동기화
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    t_send: float  # 로컬 송신 시각
    t_recv: float  # 로컬 수신 시각
    server_sec: int  # Date 헤더 (초 단위 절삭)

    @property
    def rtt(self) -> float:
        return self.t_recv - self.t_send

    @property
    def mid(self) -> float:
        return (self.t_send + self.t_recv) / 2


@dataclass
class SyncResult:
    offset: float  # 서버시각 = 로컬시각 + offset
    error: float  # 추정 오차(±초, 대칭 지연 가정)
    rtt: float  # 최소 왕복 시간
    hard_lo: float  # 지연 비대칭까지 고려한 엄밀한 하한
    hard_hi: float  # 엄밀한 상한
    samples: int


class ServerClock:
    """Date 헤더의 '초가 바뀌는 순간'을 이분 탐색해 서버 시계를 ms 단위로 추정한다.

    한 번의 요청은 "서버가 Date 를 찍은 순간은 [송신, 수신] 사이이고, 그 순간 서버 시각은
    [S, S+1) 이다" 라는 제약을 준다. 초 경계 직전/직후에 요청이 도착하도록 송신 시각을
    조절하면 이 구간이 반씩 줄어들어, 10여 번이면 RTT/2 수준까지 수렴한다.
    """

    def __init__(self, session: requests.Session, probe_url: str, clock: Clock) -> None:
        self.session = session
        self.probe_url = probe_url
        self.clock = clock
        self.samples: list[Sample] = []

    def probe(self) -> Sample:
        t_send = self.clock.now()
        resp = self.session.head(self.probe_url, allow_redirects=False, timeout=5)
        t_recv = self.clock.now()
        date = resp.headers.get("Date")
        if not date:
            raise RuntimeError(f"서버 응답에 Date 헤더가 없습니다: {self.probe_url}")
        s = Sample(t_send, t_recv, int(parsedate_to_datetime(date).timestamp()))
        self.samples.append(s)
        return s

    def estimate(self) -> SyncResult:
        if not self.samples:
            raise RuntimeError("샘플이 없습니다")
        min_rtt = min(s.rtt for s in self.samples)
        # 지연이 튄 샘플은 중간값 가정이 크게 틀리므로 추정에서 뺀다
        good = [s for s in self.samples if s.rtt <= min_rtt * 1.5 + 0.005] or self.samples
        lo = max(s.server_sec - s.mid for s in good)
        hi = min(s.server_sec + 1 - s.mid for s in good)
        hard_lo = max(s.server_sec - s.t_recv for s in self.samples)
        hard_hi = min(s.server_sec + 1 - s.t_send for s in self.samples)
        # lo > hi 면 지터로 제약이 약간 충돌한 것: 가운데를 쓰고 충돌 폭을 오차로 본다
        return SyncResult(
            offset=(lo + hi) / 2,
            error=abs(hi - lo) / 2,
            rtt=min_rtt,
            hard_lo=hard_lo,
            hard_hi=hard_hi,
            samples=len(self.samples),
        )

    def sync(self, probes: int = 10, verbose: bool = True) -> SyncResult:
        # 연결 수립(TLS 핸드셰이크) 비용을 빼기 위한 워밍업
        self.session.head(self.probe_url, allow_redirects=False, timeout=10)
        for _ in range(3):
            self.probe()
        for i in range(probes):
            est = self.estimate()
            now = self.clock.now()
            # 적어도 0.25초 뒤의 서버 초 경계를 목표로
            boundary = math.floor(now + est.offset + est.rtt + 0.25) + 1
            # 요청이 서버에 도착(≈ 송신 + RTT/2)하는 순간이 경계와 겹치도록 송신
            send_at = boundary - est.offset - est.rtt / 2
            self.clock.wait_until(send_at)
            self.probe()
            if verbose:
                e = self.estimate()
                print(
                    f"  [{i + 1:2d}/{probes}] offset={e.offset * 1000:+9.1f}ms "
                    f"±{e.error * 1000:5.1f}ms  rtt={e.rtt * 1000:6.1f}ms"
                )
        return self.estimate()


# ---------------------------------------------------------------------------
# 폼 파싱
# ---------------------------------------------------------------------------
@dataclass
class FormInfo:
    base_url: str  # .../forms/d/e/<id>
    title: str = ""
    fbzx: str | None = None
    page_count: int = 1
    email_mode: int = 0  # 0/1: 수집 안 함, 2: 인증된 이메일(로그인 필요), 3: 직접 입력
    questions: list | None = None
    accepting: bool = True
    requires_login: bool = False

    @property
    def view_url(self) -> str:
        return self.base_url + "/viewform"

    @property
    def submit_url(self) -> str:
        return self.base_url + "/formResponse"


FORM_PATH_RE = re.compile(r"^(.*?/forms/(?:u/\d+/)?d/(?:e/)?[\w-]+)")


def form_base_url(url: str) -> str:
    parts = urlsplit(url)
    m = FORM_PATH_RE.match(parts.path)
    if not m:
        raise ValueError(f"구글폼 URL 형식이 아닙니다: {url}")
    path = re.sub(r"/u/\d+/", "/", m.group(1))
    return f"{parts.scheme}://{parts.netloc}{path}"


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=4, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def extract_load_data(html: str):
    m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def parse_questions(data) -> list[dict]:
    questions = []
    items = (data[1] or [None, None])[1] or []
    for item in items:
        if not isinstance(item, list) or len(item) < 4:
            continue
        qtype = item[3]
        q = {
            "title": item[1] or "",
            "type": qtype,
            "type_name": QUESTION_TYPES.get(qtype, f"기타({qtype})"),
            "entries": [],
        }
        answers = item[4] if len(item) > 4 and isinstance(item[4], list) else []
        for ans in answers:
            if not isinstance(ans, list) or not ans:
                continue
            opts = []
            if len(ans) > 1 and isinstance(ans[1], list):
                for o in ans[1]:
                    if isinstance(o, list) and o:
                        opts.append("__other_option__" if o[0] == "" and len(o) > 4 and o[4] else o[0])
            row = ans[3][0] if len(ans) > 3 and isinstance(ans[3], list) and ans[3] else None
            q["entries"].append(
                {
                    "id": ans[0],
                    "options": opts,
                    "required": bool(ans[2]) if len(ans) > 2 else False,
                    "row": row,
                }
            )
        questions.append(q)
    return questions


def fetch_form(session: requests.Session, url: str) -> FormInfo:
    resp = session.get(url, timeout=15)
    final = resp.url
    if "accounts.google.com" in urlsplit(final).netloc or "ServiceLogin" in final:
        info = FormInfo(base_url=form_base_url(url))
        info.requires_login = True
        return info
    info = FormInfo(base_url=form_base_url(final))
    if "closedform" in final:
        info.accepting = False
    html = resp.text
    m = re.search(r'name="fbzx"\s+value="([^"]+)"', html)
    if m:
        info.fbzx = m.group(1)
    data = extract_load_data(html)
    if data is None:
        if resp.status_code >= 400:
            raise RuntimeError(f"폼 페이지를 불러오지 못했습니다 (HTTP {resp.status_code})")
        return info
    try:
        info.title = data[1][8] or data[3] or ""
    except (IndexError, TypeError):
        pass
    try:
        info.email_mode = int(data[1][10][6] or 0)
    except (IndexError, TypeError, ValueError):
        pass
    info.questions = parse_questions(data)
    info.page_count = 1 + sum(1 for q in info.questions if q["type"] == 8)
    return info


def entry_keys(q: dict, e: dict) -> list[str]:
    base = f"entry.{e['id']}"
    if q["type"] == 9:
        return [base + "_year", base + "_month", base + "_day"]
    if q["type"] == 10:
        return [base + "_hour", base + "_minute"]
    return [base]


def _norm(s: str) -> str:
    return re.sub(r"[\s*]+", "", s or "").lower()


def find_question(questions: list[dict], title: str) -> dict:
    """문항 제목으로 찾기: 정확히 일치 → 공백 무시 일치 → 유일한 부분 일치 순."""
    qs = [q for q in questions if q["entries"]]
    for cands in (
        [q for q in qs if q["title"] == title],
        [q for q in qs if _norm(q["title"]) == _norm(title)],
        [q for q in qs if _norm(title) in _norm(q["title"])],
    ):
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise ValueError(f"'{title}' 와 일치하는 문항이 여러 개입니다: {[q['title'] for q in cands]}")
    raise ValueError(f"'{title}' 문항을 찾지 못했습니다. inspect 로 문항 제목을 확인하세요.")


def resolve_answers(answers: dict, info: FormInfo) -> dict:
    """answers 의 '문항 제목' 키를 entry 키로 바꾸고, 선택지 값이 맞는지 확인한다."""
    resolved: dict = {}
    for key, value in answers.items():
        if key.startswith(("entry.", "emailAddress")) or key.isdigit():
            resolved[key if not key.isdigit() else f"entry.{key}"] = value
            continue
        if not info.questions:
            raise ValueError(
                f"문항 제목('{key}')으로 답을 지정했지만 폼 문항을 읽을 수 없습니다(폼이 닫혀 있음?). "
                "폼이 열려 있을 때 inspect 로 entry ID 를 확인해 entry 키로 적어 주세요."
            )
        q = find_question(info.questions, key)
        if len(q["entries"]) != 1:
            raise ValueError(f"'{q['title']}' 는 행이 여러 개인 문항이라 entry 키로 직접 적어야 합니다.")
        e = q["entries"][0]
        keys = entry_keys(q, e)
        if len(keys) > 1:  # 날짜 "2026-10-01" / 시간 "10:30"
            parts = re.split(r"[-./:]", str(value))
            if len(parts) != len(keys):
                raise ValueError(f"'{q['title']}' 값 형식이 잘못됐습니다: {value!r}")
            resolved.update({k: str(int(v)) for k, v in zip(keys, parts)})
            continue
        opts = [o for o in e["options"] if o != "__other_option__"]
        if opts and q["type"] in (2, 3, 4, 5):
            for v in value if isinstance(value, list) else [value]:
                if str(v) not in opts and "__other_option__" not in e["options"]:
                    raise ValueError(f"'{q['title']}' 에 '{v}' 선택지가 없습니다. 선택지: {opts}")
        resolved[keys[0]] = value
    return resolved


# ---------------------------------------------------------------------------
# 제출
# ---------------------------------------------------------------------------
def build_payload(answers: dict, info: FormInfo) -> list[tuple[str, str]]:
    payload: list[tuple[str, str]] = []
    for key, value in answers.items():
        key = key if key.startswith(("entry.", "emailAddress")) else f"entry.{key}"
        values = value if isinstance(value, list) else [value]
        for v in values:
            if v is None or v == "":
                continue
            payload.append((key, str(v)))
    payload.append(("fvv", "1"))
    payload.append(("pageHistory", ",".join(str(i) for i in range(info.page_count))))
    if info.fbzx:
        payload.append(("fbzx", info.fbzx))
    return payload


def classify(resp: requests.Response) -> str:
    """ok / closed / login / invalid / unknown"""
    text = resp.text
    if "accounts.google.com" in resp.url:
        return "login"
    if "closedform" in resp.url or "ClosedForm" in text or "더 이상 응답을 받지" in text or "no longer accepting" in text:
        return "closed"
    if resp.status_code == 200 and (
        "ResponseConfirmation" in text or "응답이 기록" in text or "response has been recorded" in text
    ):
        return "ok"
    if resp.status_code == 400 or (resp.status_code == 200 and "FB_PUBLIC_LOAD_DATA_" in text):
        return "invalid"
    if resp.status_code in (401, 403):
        return "login"
    return "ok" if resp.status_code == 200 else "unknown"


def parse_target(s: str, tz: str) -> float:
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=ZoneInfo(tz)).timestamp()
        except ValueError:
            continue
    try:  # 오프셋이 포함된 ISO 형식
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):  # 시각만 주면 오늘(지났으면 내일)
        try:
            t = datetime.strptime(s, fmt).time()
        except ValueError:
            continue
        now = datetime.now(ZoneInfo(tz))
        dt = datetime.combine(now.date(), t, tzinfo=ZoneInfo(tz))
        if dt.timestamp() < now.timestamp():
            dt = datetime.fromtimestamp(dt.timestamp() + 86400, ZoneInfo(tz))
        return dt.timestamp()
    raise ValueError(f"시각 형식을 알 수 없습니다: {s!r} (예: 2026-09-28 10:00:00)")


def fmt_ts(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def default_probe_url(form_url: str) -> str:
    p = urlsplit(form_url)
    return f"{p.scheme}://{p.netloc}/favicon.ico"


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------
def cmd_inspect(args) -> int:
    session = new_session()
    info = fetch_form(session, args.url)
    if info.requires_login:
        print("이 폼은 구글 로그인이 필요합니다. 비로그인 제출이 불가능합니다.")
        return 1
    print(f"폼 제목 : {info.title}")
    print(f"제출 URL: {info.submit_url}")
    print(f"페이지 수: {info.page_count}   fbzx: {info.fbzx or '(없음)'}")
    if not info.accepting:
        print("※ 현재 응답을 받지 않는 상태입니다(closedform). 문항 정보를 읽을 수 없습니다.")
    if info.email_mode == 2:
        print("※ '인증된 이메일 수집' 설정 폼입니다. 로그인이 필요해 비로그인 제출이 실패할 수 있습니다.")
    template: dict[str, object] = {}
    if info.email_mode == 3:
        template["emailAddress"] = ""
        print("\n[필수] emailAddress  (응답자 이메일 입력)")
    for q in info.questions or []:
        if not q["entries"]:
            continue
        print(f"\n[{q['type_name']}] {q['title']}")
        for e in q["entries"]:
            req = " (필수)" if e["required"] else ""
            row = f" 행: {e['row']}" if e["row"] else ""
            keys = entry_keys(q, e)
            print(f"  {', '.join(keys)}{row}{req}")
            if e["options"]:
                print(f"    선택지: {e['options']}")
            for k in keys:
                if q["type"] == 4:
                    template[k] = e["options"][:1]
                elif e["options"]:
                    template[k] = e["options"][0]
                else:
                    template[k] = ""
    if args.output:
        cfg = {
            "form_url": info.view_url,
            "target_time": "2026-01-01 10:00:00",
            "timezone": DEFAULT_TZ,
            "answers": template,
            "send_offset_ms": 0,
            "retry_until_open_sec": 10,
            "retry_interval_ms": 300,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"\n설정 템플릿을 저장했습니다: {args.output}  (answers 와 target_time 을 수정하세요)")
    return 0


def cmd_sync(args) -> int:
    session = new_session()
    clock = Clock()
    probe_url = args.probe_url or default_probe_url(args.url)
    print(f"서버 시간 측정 중... ({probe_url})")
    r = ServerClock(session, probe_url, clock).sync(args.probes)
    print_sync(r, clock, args.timezone)
    return 0


def print_sync(r: SyncResult, clock: Clock, tz: str) -> None:
    now = clock.now()
    print(f"\n서버 시각  : {fmt_ts(now + r.offset, tz)}")
    print(f"내 PC 시각 : {fmt_ts(now, tz)}")
    print(
        f"오차(서버-PC): {r.offset * 1000:+.1f}ms  (추정 ±{r.error * 1000:.1f}ms, "
        f"최악 [{r.hard_lo * 1000:+.1f}, {r.hard_hi * 1000:+.1f}]ms)  RTT {r.rtt * 1000:.1f}ms"
    )


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    tz = cfg.get("timezone", DEFAULT_TZ)
    form_url = cfg["form_url"]
    answers: dict = dict(cfg.get("answers") or {})
    if cfg.get("prefill_url"):  # '미리 채워진 링크' 로 답변 지정 (answers 가 우선)
        for k, v in parse_qsl(urlsplit(cfg["prefill_url"]).query):
            if k.startswith("entry.") and k not in answers:
                answers[k] = v
    target_str = args.at or cfg.get("target_time")
    offset_ms = args.offset_ms if args.offset_ms is not None else float(cfg.get("send_offset_ms", 0))
    retry_sec = float(cfg.get("retry_until_open_sec", 10))
    retry_int = float(cfg.get("retry_interval_ms", 300)) / 1000
    resync_before = float(cfg.get("resync_before_sec", 40))
    probes = int(cfg.get("sync_probes", 10))

    session = new_session()
    clock = Clock()

    print("1) 폼 정보 확인")
    info = fetch_form(session, form_url)
    if info.requires_login:
        print("   이 폼은 구글 로그인이 필요합니다. 비로그인 제출이 불가능합니다.")
        return 1
    if cfg.get("page_count"):
        info.page_count = int(cfg["page_count"])
    print(f"   제목: {info.title or '(알 수 없음)'} / 페이지 {info.page_count} / "
          f"{'응답 받는 중' if info.accepting else '현재 닫혀 있음'}")
    try:
        answers = resolve_answers(answers, info)
    except ValueError as ex:
        print(f"   ❌ {ex}")
        return 2
    missing = []
    for q in info.questions or []:
        for e in q["entries"]:
            if e["required"] and not any(answers.get(k) not in (None, "", []) for k in entry_keys(q, e)):
                missing.append(q["title"] + (f" / {e['row']}" if e["row"] else ""))
    if info.email_mode == 3 and not answers.get("emailAddress"):
        missing.append("emailAddress")
    if missing:
        print("   ⚠ 답변이 없는 필수 문항: " + ", ".join(missing))
    payload = build_payload(answers, info)
    print("   전송 데이터:", payload)

    probe_url = cfg.get("probe_url") or default_probe_url(form_url)
    sc = ServerClock(session, probe_url, clock)
    print("\n2) 서버 시간 동기화")
    r = sc.sync(probes, verbose=args.verbose)
    print_sync(r, clock, tz)

    target = time.time() + r.offset + 3 if args.now else parse_target(target_str, tz)
    print(f"\n목표 서버 시각: {fmt_ts(target, tz)} ({tz})  보정 {offset_ms:+.0f}ms")
    if target - (clock.now() + r.offset) < 0:
        print("   ⚠ 목표 시각이 이미 지났습니다. 바로 제출합니다.")

    # 오래 기다리는 경우 PC 시계 드리프트를 없애기 위해 직전에 재동기화
    resync_at = target - r.offset - resync_before
    if resync_at - clock.now() > 5:
        clock.wait_until(resync_at, on_tick=lambda rem: countdown(rem + resync_before, "재동기화 대기"))
        print("\n\n3) 직전 재동기화")
        sc.samples.clear()
        r = sc.sync(probes, verbose=args.verbose)
        print_sync(r, clock, tz)

    # 서버에 '도착'하는 시각이 목표가 되도록 편도 지연만큼 먼저 보낸다
    send_local = target - r.offset - r.rtt / 2 + offset_ms / 1000
    warm_local = send_local - 1.5
    if warm_local > clock.now():
        clock.wait_until(warm_local, on_tick=lambda rem: countdown(rem + 1.5, "제출 대기"))
        sc.probe()  # keep-alive 연결 데우기
    prepared = session.prepare_request(requests.Request("POST", info.submit_url, data=payload,
                                                        headers={"Referer": info.view_url}))
    clock.wait_until(send_local)

    if args.dry_run:
        t = clock.now()
        print(f"\n[dry-run] 이 순간 전송했을 것: 로컬 {fmt_ts(t, tz)} / 서버 도착 예상 "
              f"{fmt_ts(t + r.offset + r.rtt / 2, tz)}")
        return 0

    deadline = send_local + retry_sec
    attempt = 0
    while True:
        attempt += 1
        t0 = clock.now()
        try:
            resp = session.send(prepared, timeout=15, allow_redirects=True)
            status = classify(resp)
        except requests.RequestException as ex:
            resp, status = None, f"error: {ex}"
        t1 = clock.now()
        arrive = t0 + r.offset + r.rtt / 2
        print(f"\n#{attempt} 송신 {fmt_ts(t0 + r.offset, tz)} (서버기준) → 도착 예상 {fmt_ts(arrive, tz)}, "
              f"응답 {(t1 - t0) * 1000:.0f}ms, 결과: {status}"
              + (f" (HTTP {resp.status_code})" if resp is not None else ""))
        if status == "ok":
            print("✅ 제출 완료")
            return 0
        if status == "invalid":
            print("❌ 입력값 오류(필수 문항 누락/선택지 불일치 등). inspect 결과와 answers 를 확인하세요.")
            return 2
        if status == "login":
            print("❌ 로그인이 필요한 폼입니다.")
            return 2
        if status == "unknown":
            print("❓ 결과를 판별하지 못했습니다. 중복 제출을 막기 위해 재시도하지 않습니다.")
            print((resp.text[:500] if resp is not None else ""))
            return 3
        # closed 또는 네트워크 오류 → 폼이 열릴 때까지 재시도
        if clock.now() + retry_int > deadline:
            print("❌ 재시도 시간 초과")
            return 4
        clock.wait_until(clock.now() + retry_int)


def countdown(remain: float, label: str) -> None:
    h, rem = divmod(int(remain), 3600)
    m, s = divmod(rem, 60)
    sys.stdout.write(f"\r   {label}: {h:02d}:{m:02d}:{s:02d} 남음   ")
    sys.stdout.flush()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="구글폼 예약 제출 매크로 (비로그인, 서버시간 기준)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="폼 문항과 entry ID 확인, 설정 템플릿 생성")
    pi.add_argument("url")
    pi.add_argument("-o", "--output", help="설정 템플릿(JSON) 저장 경로")
    pi.set_defaults(func=cmd_inspect)

    ps = sub.add_parser("sync", help="구글폼 서버 시간과 내 PC 시간 차이 측정")
    ps.add_argument("url")
    ps.add_argument("--probes", type=int, default=10)
    ps.add_argument("--probe-url")
    ps.add_argument("--timezone", default=DEFAULT_TZ)
    ps.set_defaults(func=cmd_sync)

    pr = sub.add_parser("run", help="설정 파일대로 예약 제출")
    pr.add_argument("config")
    pr.add_argument("--at", help="목표 시각 (설정 파일의 target_time 대신)")
    pr.add_argument("--offset-ms", type=float, help="송신 보정(ms). +면 늦게, -면 일찍")
    pr.add_argument("--now", action="store_true", help="3초 뒤 즉시 제출(테스트용)")
    pr.add_argument("--dry-run", action="store_true", help="실제 제출 없이 타이밍만 확인")
    pr.add_argument("-v", "--verbose", action="store_true", help="동기화 과정 출력")
    pr.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n중단됨")
        return 130


if __name__ == "__main__":
    sys.exit(main())
