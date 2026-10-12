"""HTTP 공통 유틸 — Node 버전 api/lib.js 의 재시도·한도·키 마스킹 부분을 옮긴 것.

  - redact_url     : 로그에 찍힐 URL 에서 API 키를 가린다
  - Http.get       : 네트워크 오류 / 429 / 5xx 를 지수 백오프로 재시도
  - HourlyLimiter  : 시간당 호출 한도. 한도에 닿으면 남은 시간만큼만 기다린다
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

# ── 에러 타입 ───────────────────────────────────────────────────────


class HttpError(Exception):
    """응답은 받았지만 2xx 가 아닌 경우."""

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} — {redact_url(url)}")
        self.status = status
        self.body = body  # 에러 원인 판별용 (예: Pixabay 의 'Invalid or missing API key')


class NetworkError(Exception):
    """재시도를 다 쓰고도 연결하지 못한 경우."""


class SkipError(Exception):
    """이 이미지 한 장만 건너뛰면 되는 경우 (깨진 파일, 에러 페이지 등)."""


class AuthFailure(Exception):
    """API 키가 틀렸거나 권한이 없는 경우. 재시도해도 결과가 같다."""


# ── URL 마스킹 ──────────────────────────────────────────────────────

# Pixabay 처럼 API 키를 URL 쿼리로 받는 API 가 있다. 이 이름의 파라미터 값은 로그에 남기지 않는다.
_SECRET_PARAMS = {"key", "apikey", "api_key", "client_id", "client_secret", "access_token", "token"}


def redact_url(url: str) -> str:
    """https://pixabay.com/api/?key=abcd&q=cat → https://pixabay.com/api/?key=***&q=cat"""
    try:
        parts = urlsplit(url)
        query = [
            (name, "***" if name.lower() in _SECRET_PARAMS else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(parts._replace(query=urlencode(query, safe="*")))
    except ValueError:
        return "(잘못된 URL)"  # 파싱하지 못하면 원문을 내보내지 않는다


# ── 재시도 GET ──────────────────────────────────────────────────────

_RETRYABLE_STATUS = {408, 425, 429}
_MAX_RETRY_AFTER = 60  # 서버가 이보다 오래 기다리라고 하면 재시도 대신 에러로 올린다 (스케줄러가 1시간 쉼)


def _backoff(attempt: int) -> float:
    """1초 → 2초 → 4초 → 8초 (최대 30초). 지터를 더해 여러 요청이 같은 순간에 몰리지 않게 한다."""
    return min(2**attempt, 30) + random.uniform(0, 0.5)


class Http:
    """httpx.AsyncClient 를 감싸 재시도 정책을 한곳에 모은다."""

    def __init__(self, client: httpx.AsyncClient, *, retries: int, timeout: float,
                 sleep: Sleep = asyncio.sleep):
        self._client = client
        self._retries = retries
        self._timeout = timeout
        self._sleep = sleep

    async def get(self, url: str, *, headers: dict | None = None, retries: int | None = None,
                  timeout: float | None = None, label: str = "") -> httpx.Response:
        """GET 요청. 성공(2xx)하면 응답을, 아니면 HttpError / NetworkError 를 낸다."""
        retries = self._retries if retries is None else retries
        name = label or redact_url(url)

        for attempt in range(retries + 1):
            is_last = attempt == retries
            try:
                response = await self._client.get(url, headers=headers, timeout=timeout or self._timeout)
            except httpx.TransportError as err:  # 타임아웃, 연결 끊김, DNS 실패 등
                if is_last:
                    raise NetworkError(f"{type(err).__name__} — {redact_url(url)}") from err
                wait = _backoff(attempt)
                log.info("%s 재시도: %s, %.0f초 후 (%d/%d)", name, type(err).__name__, wait, attempt + 1, retries)
                await self._sleep(wait)
                continue

            if response.is_success:
                return response

            # 4xx 는 다시 보내도 결과가 같으므로 바로 실패 처리 (408·425·429 제외)
            status = response.status_code
            if status not in _RETRYABLE_STATUS and status < 500:
                raise HttpError(status, url, response.text[:300])

            # 429 는 서버가 알려준 대기 시간(Retry-After)을 우선 따른다
            retry_after = response.headers.get("retry-after", "")
            wait = float(retry_after) if retry_after.isdigit() else _backoff(attempt)
            if is_last or wait > _MAX_RETRY_AFTER:
                raise HttpError(status, url, response.text[:300])

            log.info("%s 재시도: HTTP %d, %.0f초 후 (%d/%d)", name, status, wait, attempt + 1, retries)
            await self._sleep(wait)

        raise AssertionError("unreachable")


# ── 시간당 호출 한도 ────────────────────────────────────────────────


class HourlyLimiter:
    """시간당 N회 한도. API 를 호출하기 직전에 `await limiter.take()` 를 부른다.

    한도에 닿으면 1시간을 통째로 쉬지 않고, 창(window)의 남은 시간만 기다린다.
    """

    WINDOW = 3600.0
    MARGIN = 5.0  # 서버 시계와의 오차 여유

    def __init__(self, limit: int | None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep):
        self.limit = limit
        self._clock = clock
        self._sleep = sleep
        self._count = 0
        self._window_start = clock()

    async def take(self, on_wait: Callable[[float], None] | None = None,
                   on_resume: Callable[[], None] | None = None) -> None:
        """호출 1회분을 사용한다. 기다려야 하면 on_wait(초) → 대기 → on_resume() 순으로 알린다."""
        if not self.limit:
            return

        if self._clock() - self._window_start >= self.WINDOW:
            self._reset()

        if self._count >= self.limit:
            wait = self.WINDOW - (self._clock() - self._window_start) + self.MARGIN
            if on_wait:
                on_wait(wait)
            await self._sleep(wait)
            self._reset()
            if on_resume:
                on_resume()

        self._count += 1

    def _reset(self) -> None:
        self._count = 0
        self._window_start = self._clock()
