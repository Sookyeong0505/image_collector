"""Pexels — https://www.pexels.com/api/documentation/

  - 인증  : Authorization: <API_KEY> 헤더
  - 한도  : 시간당 200회 + 월 25,000회
            응답 헤더(X-Ratelimit-*)는 '월간' 한도만 알려 준다. 시간당 횟수는 우리가 직접 센다.
  - 페이지: 응답에 next_page 가 있으면 다음 페이지가 있다
"""

import time

import httpx

from ..http import Http, RateLimiter
from .base import Page, Source

PER_PAGE = 80  # Pexels 최대값


class Pexels(Source):
    name = "pexels"
    required_env = ("PEXELS_API_KEY",)
    rate_limit = 190  # 시간당 공식 한도 200 — 여유분 확보
    item_delay = 0.15

    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        url = f"https://api.pexels.com/v1/search?query={self.q(keyword)}&page={cursor}&per_page={PER_PAGE}"
        response = await self.api_get(http, url, {"Authorization": self.env["PEXELS_API_KEY"]})
        return response.json()

    # 응답 예: { total_results, page, photos: [...], next_page: "https://..." }
    def parse_page(self, data: dict, cursor: str) -> Page:
        return Page(
            items=data.get("photos") or [],
            total=data.get("total_results"),
            next_cursor=str(int(cursor) + 1) if data.get("next_page") else None,
        )

    def read_rate_headers(self, headers: httpx.Headers, limiter: RateLimiter) -> None:
        # 월간 한도를 다 썼으면 초기화 시각(X-Ratelimit-Reset, 유닉스 시각)까지 호출을 미룬다
        remaining = self.header_number(headers, "x-ratelimit-remaining")
        reset_at = self.header_number(headers, "x-ratelimit-reset")
        if remaining is not None and remaining <= 0 and reset_at is not None:
            limiter.hold(reset_at - time.time())

    def item_id(self, item: dict) -> str:
        return str(item["id"])

    def image_url(self, item: dict) -> str | None:
        # original(원본, 매우 큼) / large2x(1880px) / large(940px)
        size = self.env.get("PEXELS_IMAGE_SIZE") or "large2x"
        sources = item.get("src") or {}
        return sources.get(size) or sources.get("original")
