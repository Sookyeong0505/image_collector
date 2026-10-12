"""테스트 공용 도구: 가짜 웹(FakeWeb), 가짜 시계(FakeClock), 테스트 이미지 생성."""

import io
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image

from app.collector.http import Http
from app.config import Settings
from app.db import connect, init_db


def make_image(seed: int, size: int = 256, quality: int = 90, fmt: str = "JPEG") -> bytes:
    """seed 마다 무늬가 다른 이미지. 같은 seed 면 크기·화질을 바꿔도 '같은 사진'이다."""
    rng = random.Random(seed)
    blocks = Image.frombytes("L", (8, 8), bytes(rng.randrange(256) for _ in range(64)))
    buffer = io.BytesIO()
    blocks.resize((size, size), Image.Resampling.BICUBIC).convert("RGB").save(buffer, fmt, quality=quality)
    return buffer.getvalue()


class FakeWeb:
    """호스트별 처리 함수를 등록해 두는 가짜 인터넷. 받은 요청을 모두 기록한다."""

    def __init__(self):
        self.routes = {}
        self.requests: list[httpx.Request] = []

    def route(self, host: str, handler) -> None:
        self.routes[host] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.host)
        return handler(request) if handler else httpx.Response(404)

    def images(self, files: dict[str, bytes], host: str = "img.test") -> None:
        """경로 → 이미지 바이트. 없는 경로는 404."""
        def handler(request):
            data = files.get(request.url.path)
            return httpx.Response(200, content=data) if data else httpx.Response(404)
        self.route(host, handler)

    def count(self, host: str) -> int:
        return sum(1 for request in self.requests if request.url.host == host)

    def paths(self, host: str) -> list[str]:
        return [str(request.url.raw_path, "ascii") for request in self.requests if request.url.host == host]


class FakeClock:
    """sleep 하면 실제로 기다리지 않고 시계만 앞으로 돌린다."""

    def __init__(self):
        self.moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleeps: list[float] = []
        self.on_sleep = None  # 잠드는 순간의 상태를 들여다보고 싶을 때

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return self.moment.timestamp()

    async def sleep(self, seconds: float) -> None:
        if self.on_sleep:
            self.on_sleep(seconds)
        self.sleeps.append(seconds)
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        base_save_path=tmp_path / "Image",
        db_path=tmp_path / "test.db",
        target_count=None,
        timeout=5,
        max_retries=1,
        phash_threshold=5,
        env={
            "UNSPLASH_API_KEY": "unsplash-secret", "PEXELS_API_KEY": "pexels-secret",
            "PIXABAY_API_KEY": "pixabay-secret", "NAVER_CLIENT_ID": "naver-id", "NAVER_CLIENT_SECRET": "naver-secret",
        },
    )


@pytest.fixture
def conn(settings):
    connection = connect(settings.db_path)
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def http(web, clock, settings) -> Http:
    client = httpx.AsyncClient(transport=httpx.MockTransport(web))
    return Http(client, retries=settings.max_retries, timeout=settings.timeout, sleep=clock.sleep)


# ── 사이트별 가짜 검색 API ──────────────────────────────────────────


def page_of(request: httpx.Request, name: str = "page") -> int:
    return int(request.url.params[name])


def fake_pexels(web: FakeWeb, pages: dict[int, list[int]]) -> None:
    """pages: {페이지 번호: [사진 ID, …]}. 이미지 주소는 https://img.test/p{ID}"""
    def handler(request):
        page = page_of(request)
        photos = [{"id": i, "src": {"large2x": f"https://img.test/p{i}"}} for i in pages.get(page, [])]
        body = {"total_results": sum(map(len, pages.values())), "photos": photos}
        if page + 1 in pages:
            body["next_page"] = "yes"
        return httpx.Response(200, json=body)
    web.route("api.pexels.com", handler)


def fake_pixabay(web: FakeWeb, pages: dict[int, list[int]], total_hits: int | None = None) -> None:
    def handler(request):
        page = page_of(request)
        if page not in pages:
            return httpx.Response(400, text='[ERROR 400] "page" is out of valid range.')
        hits = [{"id": i, "largeImageURL": f"https://img.test/x{i}"} for i in pages[page]]
        total = total_hits if total_hits is not None else sum(map(len, pages.values()))
        return httpx.Response(200, json={"total": total, "totalHits": total, "hits": hits})
    web.route("pixabay.com", handler)


def fake_naver(web: FakeWeb, links: list[str]) -> None:
    """첫 페이지(start=1)에만 결과가 있다."""
    def handler(request):
        items = [{"link": link} for link in links] if page_of(request, "start") == 1 else []
        return httpx.Response(200, json={"total": len(links), "items": items})
    web.route("naverapihub.apigw.ntruss.com", handler)


def fake_unsplash(web: FakeWeb, pages: dict[int, list[str]]) -> None:
    def handler(request):
        if request.url.path.startswith("/photos/"):
            return httpx.Response(200, json={"url": "ok"})  # 다운로드 트리거
        page = page_of(request)
        results = [
            {"id": i, "urls": {"regular": f"https://img.test/u{i}"},
             "links": {"download_location": f"https://api.unsplash.com/photos/{i}/download"}}
            for i in pages.get(page, [])
        ]
        return httpx.Response(200, json={
            "total": sum(map(len, pages.values())), "total_pages": len(pages), "results": results,
        })
    web.route("api.unsplash.com", handler)
