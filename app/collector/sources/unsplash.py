"""Unsplash — https://unsplash.com/documentation

  - 인증  : Authorization: Client-ID <ACCESS_KEY> 헤더
  - 한도  : Demo 앱 시간당 50회. 검색 호출 + 다운로드 트리거가 모두 포함된다.
            이미지 바이트 전송(images.unsplash.com CDN)은 한도에 포함되지 않는다.
  - 페이지: 응답의 total_pages 로 마지막 페이지를 안다
  - 주의  : 한도를 넘겨도 HTTP 403 이다 (본문 'Rate Limit Exceeded'). 인증 오류와 구분해야 한다.
"""

from ..http import Http, HttpError, NetworkError
from .base import Page, Source

PER_PAGE = 30


class Unsplash(Source):
    name = "unsplash"
    required_env = ("UNSPLASH_API_KEY",)
    hourly_limit = 48
    item_delay = 0.2  # CDN 부담 완화

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Client-ID {self.env['UNSPLASH_API_KEY']}"}

    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        url = f"https://api.unsplash.com/search/photos?query={self.q(keyword)}&page={cursor}&per_page={PER_PAGE}"
        response = await http.get(url, headers=self._auth, label=self.name)
        return response.json()

    # 응답 예: { total, total_pages, results: [...] }
    def parse_page(self, data: dict, cursor: str) -> Page:
        page = int(cursor)
        return Page(
            items=data.get("results") or [],
            total=data.get("total"),
            next_cursor=str(page + 1) if page < (data.get("total_pages") or 0) else None,
        )

    def item_id(self, item: dict) -> str:
        return str(item["id"])

    def image_url(self, item: dict) -> str | None:
        # full = 원본(수 MB, 끊김 잦음) / regular = 1080px
        size = self.env.get("UNSPLASH_IMAGE_SIZE") or "regular"
        urls = item.get("urls") or {}
        return urls.get(size) or urls.get("regular")

    async def after_save(self, item: dict, http: Http, take_api_slot) -> None:
        # Unsplash API 가이드라인: 사진을 내려받으면 이 주소를 호출해 알려야 한다 (한도에 포함).
        # 중복으로 판정돼 저장하지 않은 사진에는 호출되지 않으므로 한도를 아낀다.
        location = (item.get("links") or {}).get("download_location")
        if not location:
            return
        await take_api_slot()
        try:
            await http.get(location, headers=self._auth, label=self.name)
        except (HttpError, NetworkError):
            pass  # 알림이 실패해도 이미 저장한 파일은 유효하다

    def is_auth_error(self, err: HttpError) -> bool:
        if err.status == 403 and "rate limit" in err.body.lower():
            return False  # 한도 초과 — 스케줄러가 1시간 쉬게 둔다
        return err.status in (401, 403)
