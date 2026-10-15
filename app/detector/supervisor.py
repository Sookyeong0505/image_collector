"""검출 워커 관리 (명세 5-2). 수집기(serve) 안에서 돈다.

스위치가 켜져 있고, 동작 시간 안이고, 검출할 이미지가 있으면 워커 프로세스(python -m app detect)를 띄운다.
끄는 쪽은 워커가 스스로 한다. 워커는 이미지 1장마다 같은 조건을 보고, 아니면 그 장을 마치고 종료한다.

이 모듈은 torch 같은 무거운 패키지를 불러오지 않는다. 수집기만 쓰는 PC 에서도 import 할 수 있어야 한다.
"""

import asyncio
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, time, timedelta

from ..config import PROJECT_ROOT
from ..db import repo

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30          # 초. 이 간격으로 조건을 다시 본다 (설정을 바꾸면 바로 본다)
RESTART_DELAY = 600          # 초. 워커가 비정상 종료하면 이만큼 기다렸다가 다시 띄운다
EXIT_LOCKED = 3              # 워커 종료 코드: 다른 워커가 이미 돌고 있다

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str) -> time:
    """'19:00' → time(19, 0). 형식이 틀리면 ValueError."""
    match = _HHMM.match(text or "")
    if not match:
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다 (예: 19:00): {text!r}")
    return time(int(match[1]), int(match[2]))


def in_window(moment: time, start: str, end: str) -> bool:
    """moment 가 동작 시간 안인가. 시작 ≤ moment < 끝. 자정을 넘는 구간과 하루 종일(시작 = 끝)을 지원한다."""
    begin, finish = parse_hhmm(start), parse_hhmm(end)
    if begin == finish:
        return True
    if begin < finish:
        return begin <= moment < finish
    return moment >= begin or moment < finish


def detection_allowed(settings: dict, now_local: datetime) -> bool:
    """스위치가 켜져 있고 동작 시간 안인가. settings 는 repo.get_detector_settings 의 결과."""
    return settings["enabled"] and in_window(now_local.time(), settings["window_start"], settings["window_end"])


def spawn_worker() -> subprocess.Popen:
    """python -m app detect 를 띄운다. 콘솔 없이 실행되는 수집기(pythonw)에서도 창이 뜨지 않게 한다."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, "-m", "app", "detect"], cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


class DetectorSupervisor:
    def __init__(self, conn, *, spawn: Callable[[], subprocess.Popen] = spawn_worker,
                 now: Callable[[], datetime] = datetime.now):
        """now: 현지 시각. 테스트에서 가짜 시계와 가짜 spawn 을 넣는다."""
        self.conn = conn
        self.spawn = spawn
        self.now = now
        self.process: subprocess.Popen | None = None
        self.last_exit: dict | None = None        # 마지막 비정상 종료 {code, at}
        self.retry_after: datetime | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    @property
    def worker_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> dict:
        """화면용: 설정, 지금 상태, 마지막 비정상 종료."""
        settings = repo.get_detector_settings(self.conn)
        now = self.now()
        allowed = detection_allowed(settings, now)
        if self.worker_running:
            state = "running"         # 검출 중
        elif not settings["enabled"]:
            state = "off"             # 꺼짐
        elif not allowed:
            state = "outside"         # 동작 시간 밖
        elif self.retry_after and now < self.retry_after:
            state = "retry"           # 비정상 종료 뒤 다시 시작 대기
        elif repo.has_detection_work(self.conn):
            state = "starting"        # 곧 시작
        else:
            state = "idle"            # 할 일 없음
        return {
            **settings, "state": state,
            "in_window": in_window(now.time(), settings["window_start"], settings["window_end"]),
            "last_exit": self.last_exit,
            "retry_after": self.retry_after.strftime("%H:%M") if self.retry_after and state == "retry" else None,
        }

    def check(self) -> None:
        """조건을 보고 필요하면 워커를 띄운다. 이벤트 루프에서 30초마다, 그리고 설정이 바뀌면 부른다."""
        now = self.now()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return                # 아직 일하는 중
            self.process = None
            if code not in (0, EXIT_LOCKED):
                self.last_exit = {"code": code, "at": now.strftime("%m-%d %H:%M")}
                self.retry_after = now + timedelta(seconds=RESTART_DELAY)
                log.warning("검출 워커가 비정상 종료했습니다 (코드 %s). %d분 뒤 다시 시작합니다.", code, RESTART_DELAY // 60)

        if self.retry_after and now < self.retry_after:
            return
        settings = repo.get_detector_settings(self.conn)
        if detection_allowed(settings, now) and repo.has_detection_work(self.conn):
            log.info("검출 워커를 시작합니다")
            self.process = self.spawn()

    def wake(self) -> None:
        """설정이나 검출 대상이 바뀌었을 때 30초를 기다리지 않고 바로 확인한다."""
        self._wake.set()

    def clear_retry(self) -> None:
        """사용자가 스위치를 다시 켜면 비정상 종료 대기를 풀어 준다."""
        self.retry_after = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """수집기가 끝날 때. 띄운 워커도 함께 끝낸다 (이미지 1장의 기록은 한 트랜잭션이라 안전하다)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.worker_running:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    async def _loop(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("검출 워커 관리 중 오류")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), CHECK_INTERVAL)
            except TimeoutError:
                pass
