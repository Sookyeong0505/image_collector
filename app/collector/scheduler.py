"""스케줄러 — 어떤 작업을 언제 돌릴지 정한다.

사이트마다 태스크를 하나씩 띄우고(B안), 각 태스크는 자기 사이트의 작업을 키워드 순서대로 처리한다.
사이트끼리는 서로 기다리지 않는다. Unsplash 가 한도에 걸려 쉬는 동안에도 Pexels 는 다음 키워드로 간다.

    unsplash 태스크:  beach ──(한도 대기)── beach ── sunset ── …
    pexels   태스크:  beach ── sunset ── forest ── …
                              └ beach 를 모든 사이트가 끝내는 순간 → 검출 작업 등록

엔진이 올린 결과에 따라 작업 상태를 바꾼다.

    정상 종료        → done
    AuthFailure     → paused (1시간 뒤 재시도), 3번째에 failed
    그 밖의 예외     → paused (10분 뒤 재시도, 횟수 제한 없음)
    중단 요청        → pending (커서가 저장돼 있어 다음에 이어서 한다)
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from ..config import Settings
from ..db import repo
from ..keep_awake import KeepAwake
from ..timeutil import from_db, to_db, utcnow
from .dedup import PhashIndex
from .engine import Deps, run_job
from .http import AuthFailure, Http, RateLimiter, Sleep
from .sources import Source

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, sources: dict[str, Source], http: Http, *,
                 clock: Callable[[], datetime] = utcnow, sleep: Sleep = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic, keep_awake: KeepAwake | None = None):
        self.conn = conn
        self.settings = settings
        self.sources = sources          # 키가 설정된 사이트만 넘겨받는다
        self.clock = clock
        self.sleep = sleep
        self.keep_awake = keep_awake or KeepAwake()

        self.deps = Deps(conn, settings, http, PhashIndex.load(conn, settings.phash_threshold), sleep, clock)

        # 한도는 사이트 단위라서 키워드가 바뀌어도 같은 제한기를 계속 쓴다.
        # 사이트 명세에도 붙여 준다 → 명세가 응답 헤더를 읽어 제한기를 서버 기준으로 맞춘다.
        self.limiters: dict[str, RateLimiter] = {}
        for name, source in sources.items():
            limiter = RateLimiter(source.rate_limit, source.rate_window, clock=monotonic, sleep=sleep)
            self.limiters[name] = source.limiter = limiter
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake = {name: asyncio.Event() for name in sources}

    # ── 실행 제어 ───────────────────────────────────────────────────

    async def run_until_idle(self) -> None:
        """할 일이 없어질 때까지 돌리고 끝낸다 (CLI 용). 쉬는 작업이 있으면 깨어날 때까지 기다린다."""
        await asyncio.gather(*(self._site_loop(site, exit_when_idle=True) for site in self.sources))

    def start(self) -> None:
        """사이트별 태스크를 띄운다 (서버용). 할 일이 없으면 새 작업이 들어올 때까지 잠든다."""
        for site in self.sources:
            if site not in self._tasks or self._tasks[site].done():
                self._tasks[site] = asyncio.create_task(self._site_loop(site, exit_when_idle=False), name=site)

    async def stop(self) -> None:
        """모든 태스크를 멈춘다. 돌던 작업은 pending 으로 돌아가 다음 start() 때 이어진다."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.keep_awake.update(False)   # 사용자가 멈췄으니 PC 가 절전에 들어가도 된다

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks.values())

    def wake(self) -> None:
        """새 작업이 등록됐음을 알린다. 잠든 태스크가 바로 일어나 확인한다."""
        for event in self._wake.values():
            event.set()

    # ── 사이트 하나의 루프 ──────────────────────────────────────────

    async def _site_loop(self, site: str, exit_when_idle: bool) -> None:
        while True:
            # 작업을 하나 끝낼 때마다, 그리고 잠들기 직전에 여기를 지난다.
            # 쉬는 작업(10분·1시간 뒤 재개)도 '남은 작업'이므로 그동안 PC 는 깨어 있다.
            self.keep_awake.update(repo.count_unfinished_jobs(self.conn) > 0)

            now = self.clock()
            job = repo.next_runnable_job(self.conn, site, to_db(now))
            if job is not None:
                await self._run_one(job, self.sources[site])
                continue

            # 지금 할 수 있는 작업이 없다. 쉬는 작업이 있으면 그때까지, 없으면 새 작업이 올 때까지 기다린다.
            resume_at = repo.earliest_resume(self.conn, site)
            if resume_at is None and exit_when_idle:
                return
            delay = None if resume_at is None else max((from_db(resume_at) - now).total_seconds(), 0) + 1

            if exit_when_idle:
                await self.sleep(delay)
            else:
                await self._wait_for_wake(site, delay)

    async def _wait_for_wake(self, site: str, timeout: float | None) -> None:
        event = self._wake[site]
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        event.clear()

    # ── 작업 하나 실행 + 결과에 따른 상태 전이 ──────────────────────

    async def _run_one(self, job: sqlite3.Row, source: Source) -> None:
        conn, job_id, label = self.conn, job["id"], f"[{source.name}] '{job['keyword']}'"
        repo.mark_running(conn, job_id, to_db(self.clock()))

        try:
            await run_job(job_id, source, self.limiters[source.name], self.deps)

        except asyncio.CancelledError:
            repo.set_status(conn, job_id, "pending")
            raise

        except AuthFailure as err:
            failures = repo.add_auth_failure(conn, job_id)
            if failures >= self.settings.max_auth_failures:
                log.error("%s 인증 오류 %d회 — 실패 처리: %s", label, failures, err)
                repo.set_status(conn, job_id, "failed", last_error=str(err), finished_at=to_db(self.clock()))
            else:
                log.error("%s 인증 오류 (%d/%d) — 1시간 뒤 재시도: %s",
                          label, failures, self.settings.max_auth_failures, err)
                self._pause(job_id, err, self.settings.auth_pause_seconds)

        except Exception as err:  # 무인 운영이 목적이므로 어떤 에러든 태스크를 죽이지 않고 쉬었다 다시 한다
            log.exception("%s 오류 — 10분 뒤 재시도: %s", label, err)
            self._pause(job_id, err, self.settings.pause_seconds)

        else:
            repo.set_status(conn, job_id, "done", finished_at=to_db(self.clock()))
            log.info("%s 완료", label)

        # 이 키워드를 모든 사이트가 끝냈으면 검출 작업을 등록한다
        if repo.complete_keyword_if_finished(conn, job["keyword_id"]):
            log.info("'%s' 수집 완료 — 검출 작업 등록", job["keyword"])

    def _pause(self, job_id: int, err: Exception, seconds: int) -> None:
        resume_at = self.clock() + timedelta(seconds=seconds)
        repo.set_status(self.conn, job_id, "paused", resume_at=to_db(resume_at),
                        last_error=f"{type(err).__name__}: {err}")
