"""사이트 명세의 공통 틀 — Node 버전의 SourceSpec 에 해당한다.

엔진(engine.py)은 '흐름'만 알고, 사이트마다 다른 부분은 여기 정의한 메서드로 물어본다.
새 사이트를 추가하려면 Source 를 상속해 아래 4개만 채우면 된다:
    fetch_page / parse_page / item_id / image_url
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ...config import Settings
from ..http import Http, HttpError, RateLimiter


@dataclass(frozen=True)
class Page:
    items: list[Any]          # 검색 결과 아이템들 (사이트가 준 JSON 조각 그대로)
    total: int | None         # 이 키워드로 받을 수 있는 전체 수 (진행바 분모)
    next_cursor: str | None   # 다음에 요청할 위치. 더 없으면 None


class Source:
    name: str = ""
    required_env: tuple[str, ...] = ()   # 이 사이트에 필요한 .env 키
    rate_limit: int | None = None        # rate_window 초 동안 쓸 API 호출 수. None = 제한 없음
    rate_window: float = 3600.0
    first_cursor: str = "1"
    item_delay: float = 0.0              # 이미지 1장 받은 뒤 쉬는 시간(초)
    page_delay: float = 0.0              # 검색 페이지 1개 처리한 뒤 쉬는 시간(초)

    # 이미지 다운로드에만 적용되는 옵션 (None 이면 공통 설정을 쓴다)
    image_headers: dict[str, str] | None = None
    image_retries: int | None = None
    image_timeout: float | None = None
    min_bytes: int = 2048

    def __init__(self, settings: Settings):
        self.settings = settings
        self.env = settings.env
        self.limiter: RateLimiter | None = None   # 스케줄러가 붙여 준다

    @property
    def configured(self) -> bool:
        """.env 에 이 사이트의 키가 모두 채워져 있는지."""
        return all(self.env.get(key) for key in self.required_env)

    # ── 사이트마다 반드시 구현 ──────────────────────────────────────

    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        """검색 API 를 호출해 응답 JSON 을 돌려준다."""
        raise NotImplementedError

    def parse_page(self, data: dict, cursor: str) -> Page:
        """응답 JSON 에서 아이템 · 전체 개수 · 다음 커서를 꺼낸다."""
        raise NotImplementedError

    def item_id(self, item: Any) -> str:
        """아이템의 고유 ID. 파일명과 L0 중복 검사의 기준이 된다."""
        raise NotImplementedError

    def image_url(self, item: Any) -> str | None:
        """내려받을 이미지 URL. 없으면 그 아이템은 건너뛴다."""
        raise NotImplementedError

    # ── 필요한 사이트만 재정의 ──────────────────────────────────────

    async def after_save(self, item: Any, http: Http, take_api_slot) -> None:
        """새 이미지를 저장한 직후 할 일 (예: Unsplash 다운로드 트리거)."""

    def read_rate_headers(self, headers: httpx.Headers, limiter: RateLimiter) -> None:
        """응답 헤더에 남은 호출 횟수가 있으면 limiter 에 알려 준다. 헤더 이름과 뜻이 사이트마다 다르다."""

    def is_auth_error(self, err: HttpError) -> bool:
        """검색 API 에러가 '키가 틀림'을 뜻하는지. 3번 쌓이면 작업이 실패 처리된다."""
        return err.status in (401, 403)

    def is_skippable(self, err: HttpError) -> bool:
        """이미지 다운로드 에러를 '실패'가 아닌 '건너뜀'으로 셀지."""
        return False

    def is_end_of_results(self, err: HttpError, cursor: str) -> bool:
        """검색 API 에러가 '결과 끝'을 뜻하는지."""
        return False

    # ── 공통 도우미 ─────────────────────────────────────────────────

    async def api_get(self, http: Http, url: str, headers: dict | None = None) -> httpx.Response:
        """이 사이트의 API 를 호출한다. 응답이 오면 헤더의 한도 정보를 제한기에 반영한다."""
        response = await http.get(url, headers=headers, label=self.name)
        if self.limiter is not None:
            self.read_rate_headers(response.headers, self.limiter)
        return response

    @staticmethod
    def header_number(headers: httpx.Headers, name: str) -> float | None:
        """숫자 헤더를 읽는다. 없거나 숫자가 아니면 None."""
        try:
            return float(headers[name])
        except (KeyError, ValueError):
            return None

    async def check(self, http: Http) -> None:
        """API 연결 확인: 검색을 한 번 해 본다. 문제가 있으면 예외가 난다."""
        data = await self.fetch_page(self.first_cursor, "test", http)
        self.parse_page(data, self.first_cursor)

    @staticmethod
    def q(text: str) -> str:
        """검색어를 URL 에 넣을 수 있게 인코딩한다."""
        return quote(text, safe="")
