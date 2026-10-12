"""Pexels — https://www.pexels.com/api/documentation/

  - 인증  : Authorization: <API_KEY> 헤더
  - 한도  : 시간당 200회
  - 페이지: 응답에 next_page 가 있으면 다음 페이지가 있다
"""

from ..http import Http
from .base import Page, Source

PER_PAGE = 80  # Pexels 최대값


class Pexels(Source):
    name = "pexels"
    required_env = ("PEXELS_API_KEY",)
    hourly_limit = 190  # 공식 한도 200 — 여유분 확보
    item_delay = 0.15

    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        url = f"https://api.pexels.com/v1/search?query={self.q(keyword)}&page={cursor}&per_page={PER_PAGE}"
        response = await http.get(url, headers={"Authorization": self.env["PEXELS_API_KEY"]}, label=self.name)
        return response.json()

    # 응답 예: { total_results, page, photos: [...], next_page: "https://..." }
    def parse_page(self, data: dict, cursor: str) -> Page:
        return Page(
            items=data.get("photos") or [],
            total=data.get("total_results"),
            next_cursor=str(int(cursor) + 1) if data.get("next_page") else None,
        )

    def item_id(self, item: dict) -> str:
        return str(item["id"])

    def image_url(self, item: dict) -> str | None:
        # original(원본, 매우 큼) / large2x(1880px) / large(940px)
        size = self.env.get("PEXELS_IMAGE_SIZE") or "large2x"
        sources = item.get("src") or {}
        return sources.get(size) or sources.get("original")
