"""네이버 이미지 검색 (NCP API Gateway)

  - 인증  : X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY 헤더
  - 페이지: page 번호가 아니라 start 오프셋 방식 (1, 101, 201 … 최대 1000)
  - 특징  : 결과 이미지는 남의 블로그·카페 서버에 있다
            → 고유 ID 가 없어서 URL 의 MD5 해시를 ID 로 쓴다
            → 핫링크 차단(403)·삭제(404)가 흔해서 실패가 아닌 '건너뜀'으로 센다
"""

import hashlib

from ..http import Http, HttpError
from .base import Page, Source

API_BASE = "https://naverapihub.apigw.ntruss.com/search/v1/image"
DISPLAY = 100     # 한 번에 불러올 개수 (최대 100)
MAX_START = 1000  # start 파라미터 최대값


class Naver(Source):
    name = "naver"
    required_env = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")
    page_delay = 0.5  # 한도 정보를 주지 않는 API 라서 페이지마다 잠깐 쉰다

    # Referer / User-Agent 를 넣어야 핫링크 차단(403)을 상당수 피할 수 있다
    image_headers = {
        "Referer": "https://search.naver.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    # 남의 서버라 재시도를 짧게 가져간다
    image_retries = 2
    image_timeout = 30.0
    min_bytes = 1024

    # 커서 = start 오프셋
    async def fetch_page(self, cursor: str, keyword: str, http: Http) -> dict:
        url = f"{API_BASE}?query={self.q(keyword)}&display={DISPLAY}&start={cursor}&filter=large&format=json"
        headers = {
            "X-NCP-APIGW-API-KEY-ID": self.env["NAVER_CLIENT_ID"],
            "X-NCP-APIGW-API-KEY": self.env["NAVER_CLIENT_SECRET"],
        }
        response = await self.api_get(http, url, headers)
        return response.json()

    # 응답 예: { total, start, display, items: [{ link, thumbnail, ... }] }
    def parse_page(self, data: dict, cursor: str) -> Page:
        next_start = int(cursor) + DISPLAY
        return Page(
            items=data.get("items") or [],
            total=data.get("total"),
            next_cursor=str(next_start) if next_start <= MAX_START else None,
        )

    def item_id(self, item: dict) -> str:
        return hashlib.md5(item["link"].encode()).hexdigest()

    def image_url(self, item: dict) -> str | None:
        return item.get("link")

    def is_skippable(self, err: HttpError) -> bool:
        return err.status < 500  # 403 핫링크 차단, 404 삭제됨 등
