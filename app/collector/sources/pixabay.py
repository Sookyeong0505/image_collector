"""Pixabay — https://pixabay.com/api/docs/

  - 인증  : URL 쿼리 ?key=<API_KEY>  (로그에는 http.redact_url 이 가려서 찍는다)
  - 한도  : 분당 100회
  - 페이지: totalHits = API 로 실제 받을 수 있는 수 (보통 500장 상한)
            범위를 넘는 페이지를 요청하면 HTTP 400 을 돌려준다
  - 주의  : API 키가 틀려도 HTTP 400 이다. 응답 본문으로 구분해야 한다.
"""

from ..http import Http, HttpError
from .base import Page, Source

PER_PAGE = 200  # Pixabay 최대값


class Pixabay(Source):
    name = "pixabay"
    required_env = ("PIXABAY_API_KEY",)
    hourly_limit = 5000
    item_delay = 0.15

    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        url = (
            f"https://pixabay.com/api/?key={self.env['PIXABAY_API_KEY']}&q={self.q(keyword)}"
            f"&image_type=photo&page={cursor}&per_page={PER_PAGE}"
        )
        response = await http.get(url, label=self.name)
        return response.json()

    # 응답 예: { total, totalHits, hits: [...] }
    # 지금까지 본 개수(page * PER_PAGE)가 totalHits 에 닿으면 마지막 페이지다.
    def parse_page(self, data: dict, cursor: str) -> Page:
        page, accessible = int(cursor), data.get("totalHits") or 0
        return Page(
            items=data.get("hits") or [],
            total=accessible,
            next_cursor=str(page + 1) if page * PER_PAGE < accessible else None,
        )

    def item_id(self, item: dict) -> str:
        return str(item["id"])

    def image_url(self, item: dict) -> str | None:
        return item.get("largeImageURL") or item.get("webformatURL")

    def is_auth_error(self, err: HttpError) -> bool:
        # 실제 응답: [ERROR 400] Invalid or missing API key (https://pixabay.com/api/docs/).
        return err.status in (401, 403) or (err.status == 400 and "api key" in err.body.lower())

    def is_end_of_results(self, err: HttpError, cursor: str) -> bool:
        # 페이지 범위 초과(400)는 에러가 아니라 "결과 끝" 신호. 첫 페이지의 400 은 진짜 에러다.
        return err.status == 400 and int(cursor) > 1 and not self.is_auth_error(err)
