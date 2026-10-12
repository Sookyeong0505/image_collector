"""호출 한도: 서버가 응답 헤더로 알려 주는 남은 횟수에 맞춰 동작하는지."""

import asyncio

import httpx

from app.collector.http import RateLimiter
from app.collector.scheduler import Scheduler
from app.collector.sources import build_sources
from app.db import repo
from app.keep_awake import KeepAwake
from tests.conftest import make_image, page_of


def run_takes(limiter: RateLimiter, times: int) -> list[float]:
    """take() 를 times 번 부르고, 그동안 기다린 시간들을 돌려준다."""
    waits: list[float] = []

    async def scenario():
        for _ in range(times):
            await limiter.take(waits.append)

    asyncio.run(scenario())
    return waits


# ── 제한기 단위 동작 ────────────────────────────────────────────────


def test_재시작_직후에도_서버가_알려_준_사용량을_따른다(clock):
    """프로그램을 막 켜서 0회라고 알고 있어도, 서버가 47회 썼다고 하면 그만큼 쓴 것으로 본다."""
    limiter = RateLimiter(48, clock=clock.monotonic, sleep=clock.sleep)
    assert limiter.remaining == 48

    limiter.sync(limit=50, remaining=3)          # 서버: 50회 중 3회 남음 = 47회 사용

    assert limiter.remaining == 1                # 우리 한도 48 기준으로 1회 남음
    waits = run_takes(limiter, 2)                # 1회는 바로, 2회째는 대기
    assert len(waits) == 1 and waits[0] > 3000


def test_서버가_초기화_시각을_알려_주면_그때까지만_기다린다(clock):
    limiter = RateLimiter(95, 60, clock=clock.monotonic, sleep=clock.sleep)

    limiter.sync(limit=100, remaining=5, reset_in=20)   # 95회 사용, 20초 뒤 초기화

    assert run_takes(limiter, 1) == [20 + 5]             # 60초가 아니라 20초 + 여유 5초


def test_서버_쪽에서_창이_새로_시작되면_다시_쓸_수_있다(clock):
    limiter = RateLimiter(48, clock=clock.monotonic, sleep=clock.sleep)
    limiter.sync(limit=50, remaining=2)
    assert limiter.remaining == 0

    limiter.sync(limit=50, remaining=49)         # 서버에서 한도가 초기화된 뒤의 첫 응답

    assert limiter.remaining == 47
    assert run_takes(limiter, 1) == []


def test_초기화_시각이_지나면_기다리지_않는다(clock):
    limiter = RateLimiter(95, 60, clock=clock.monotonic, sleep=clock.sleep)
    limiter.sync(limit=100, remaining=0, reset_in=20)

    asyncio.run(clock.sleep(30))
    clock.sleeps.clear()

    assert limiter.remaining == 95
    assert run_takes(limiter, 1) == []


def test_다른_종류의_한도가_소진되면_그만큼_미룬다(clock):
    limiter = RateLimiter(190, clock=clock.monotonic, sleep=clock.sleep)

    limiter.hold(500)                            # 예: Pexels 월간 한도 소진, 500초 뒤 초기화

    assert run_takes(limiter, 2) == [500 + 5]    # 한 번 기다린 뒤에는 정상


def test_한도가_없는_사이트는_헤더를_무시한다(clock):
    limiter = RateLimiter(None, clock=clock.monotonic, sleep=clock.sleep)

    limiter.sync(limit=50, remaining=0)

    assert limiter.remaining is None
    assert run_takes(limiter, 5) == []


# ── 실제 흐름: 서버 헤더를 읽어 한도를 넘기지 않는다 ────────────────


class QuotaServer:
    """한도를 실제로 세는 가짜 API 서버. 응답마다 남은 횟수를 헤더로 알려 준다."""

    def __init__(self, clock, limit: int, window: float, used: int = 0, reset_header: bool = False):
        self.clock, self.limit, self.window, self.used = clock, limit, window, used
        self.reset_header = reset_header
        self.window_start = clock.monotonic()
        self.rejected = 0

    def count(self) -> httpx.Response | None:
        """요청 1건을 센다. 한도를 넘었으면 거절 응답을, 아니면 None 을 돌려준다."""
        if self.clock.monotonic() - self.window_start >= self.window:
            self.used, self.window_start = 0, self.clock.monotonic()
        if self.used >= self.limit:
            self.rejected += 1
            return httpx.Response(403, text="Rate Limit Exceeded")
        self.used += 1
        return None

    def headers(self) -> dict[str, str]:
        headers = {"X-Ratelimit-Limit": str(self.limit), "X-Ratelimit-Remaining": str(self.limit - self.used)}
        if self.reset_header:
            left = self.window - (self.clock.monotonic() - self.window_start)
            headers["X-Ratelimit-Reset"] = str(int(left))
        return headers


def make_scheduler(conn, settings, http, clock, site: str) -> Scheduler:
    sources = {site: build_sources(settings)[site]}
    return Scheduler(conn, settings, sources, http, clock=clock.now, sleep=clock.sleep,
                     monotonic=clock.monotonic, keep_awake=KeepAwake(lambda state: None))


def test_Unsplash_다른_곳에서_이미_쓴_한도를_넘기지_않는다(conn, settings, http, clock, web):
    """재시작했거나 같은 키를 다른 곳에서 써서, 우리가 모르는 사이 45회가 이미 쓰인 상황."""
    server = QuotaServer(clock, limit=50, window=3600, used=45)

    def unsplash(request):
        rejected = server.count()
        if rejected:
            return rejected
        if request.url.path.startswith("/photos/"):
            return httpx.Response(200, json={}, headers=server.headers())
        results = [{"id": f"u{i}", "urls": {"regular": f"https://img.test/u{i}"},
                    "links": {"download_location": f"https://api.unsplash.com/photos/u{i}/download"}}
                   for i in range(10)]
        return httpx.Response(200, json={"total": 10, "total_pages": 1, "results": results}, headers=server.headers())

    web.route("api.unsplash.com", unsplash)
    web.images({f"/u{i}": make_image(i) for i in range(10)})
    repo.add_keywords(conn, ["beach"], ["unsplash"], None)

    asyncio.run(make_scheduler(conn, settings, http, clock, "unsplash").run_until_idle())

    job = conn.execute("SELECT * FROM collect_jobs").fetchone()
    assert (job["status"], job["downloaded"]) == ("done", 10)
    assert server.rejected == 0                                  # 한 번도 한도를 넘겨 거절당하지 않았다
    assert len([s for s in clock.sleeps if s > 3000]) == 1       # 대신 한도 직전에 한 번 기다렸다
    assert job["auth_failures"] == 0 and job["last_error"] is None


def test_Pixabay는_서버가_알려_준_초만큼만_기다린다(conn, settings, http, clock, web):
    server = QuotaServer(clock, limit=100, window=60, used=94, reset_header=True)

    def pixabay(request):
        rejected = server.count()
        if rejected:
            return httpx.Response(429, text="rate limit")
        page = page_of(request)
        hits = [{"id": page, "largeImageURL": f"https://img.test/x{page}"}]
        return httpx.Response(200, json={"total": 600, "totalHits": 600, "hits": hits}, headers=server.headers())

    web.route("pixabay.com", pixabay)
    web.images({f"/x{i}": make_image(i) for i in (1, 2, 3)})
    repo.add_keywords(conn, ["beach"], ["pixabay"], None)

    asyncio.run(make_scheduler(conn, settings, http, clock, "pixabay").run_until_idle())

    job = conn.execute("SELECT * FROM collect_jobs").fetchone()
    assert (job["status"], job["downloaded"]) == ("done", 3)
    assert server.rejected == 0
    long_waits = [s for s in clock.sleeps if s > 10]
    assert len(long_waits) == 1 and long_waits[0] <= 65          # 1시간이 아니라 1분 안쪽


def test_Pexels_월간_한도가_소진되면_초기화_시각까지_미룬다(conn, settings, http, clock, web, monkeypatch):
    import app.collector.sources.pexels as pexels_module
    monkeypatch.setattr(pexels_module.time, "time", lambda: 1_000_000.0)
    calls = {"n": 0}

    def pexels(request):
        calls["n"] += 1
        page = page_of(request)
        # 첫 응답에서 '월간 0회 남음, 7200초 뒤 초기화'를 알린다
        headers = {"X-Ratelimit-Limit": "25000", "X-Ratelimit-Remaining": "0" if calls["n"] == 1 else "24999",
                   "X-Ratelimit-Reset": str(1_000_000 + 7200)}
        body = {"total_results": 2, "photos": [{"id": page, "src": {"large2x": f"https://img.test/p{page}"}}]}
        if page == 1:
            body["next_page"] = "y"
        return httpx.Response(200, json=body, headers=headers)

    web.route("api.pexels.com", pexels)
    web.images({"/p1": make_image(1), "/p2": make_image(2)})
    repo.add_keywords(conn, ["beach"], ["pexels"], None)

    asyncio.run(make_scheduler(conn, settings, http, clock, "pexels").run_until_idle())

    job = conn.execute("SELECT * FROM collect_jobs").fetchone()
    assert (job["status"], job["downloaded"]) == ("done", 2)
    # 2페이지를 요청하기 전에 초기화 시각까지 대기 (그사이 이미지 저장 후 쉰 0.15초만큼은 이미 지나 있다)
    long_waits = [s for s in clock.sleeps if s > 3000]
    assert len(long_waits) == 1 and 7200 <= long_waits[0] <= 7205
