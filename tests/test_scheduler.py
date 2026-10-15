"""스케줄러: 여러 키워드·사이트를 어떤 순서로 돌리고, 에러에 어떻게 대응하는지.

시간은 FakeClock 이 대신 흘려보내므로 '1시간 대기'도 즉시 끝난다.
"""

import asyncio

import httpx
import pytest

from app.collector.scheduler import Scheduler
from app.collector.sources import build_sources
from app.db import repo
from app.timeutil import from_db
from tests.conftest import fake_pexels, fake_pixabay, fake_unsplash, make_image, page_of


@pytest.fixture
def make_scheduler(conn, settings, http, clock):
    def _make(*sites: str) -> Scheduler:
        sources = {name: source for name, source in build_sources(settings).items() if name in sites}
        return Scheduler(conn, settings, sources, http, clock=clock.now, sleep=clock.sleep, monotonic=clock.monotonic)
    return _make


def jobs(conn) -> dict[tuple[str, str], dict]:
    """{(키워드, 사이트): 작업 행}"""
    rows = conn.execute(
        "SELECT j.*, k.text AS keyword FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id"
    ).fetchall()
    return {(row["keyword"], row["site"]): dict(row) for row in rows}


def keyword_status(conn) -> dict[str, str]:
    return {row["text"]: row["status"] for row in conn.execute("SELECT text, status FROM keywords")}


def detection_jobs(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT d.*, k.text AS keyword FROM detection_jobs d JOIN keywords k ON k.id = d.keyword_id"
    ).fetchall()
    return {row["keyword"]: dict(row) for row in rows}


def searched_keywords(web, host: str, param: str = "query") -> list[str]:
    return [request.url.params[param] for request in web.requests if request.url.host == host]


# ── 정상 흐름 ───────────────────────────────────────────────────────


def test_모든_사이트를_끝내면_수집_완료가_되고_검출은_자동으로_등록하지_않는다(make_scheduler, web, conn):
    fake_pexels(web, {1: [1, 2]})
    fake_pixabay(web, {1: [7]})
    web.images({"/p1": make_image(1), "/p2": make_image(2), "/x7": make_image(3)})
    repo.add_keywords(conn, ["beach", "sunset"], ["pexels", "pixabay"], None)

    asyncio.run(make_scheduler("pexels", "pixabay").run_until_idle())

    assert {job["status"] for job in jobs(conn).values()} == {"done"}
    assert keyword_status(conn) == {"beach": "collected", "sunset": "collected"}
    assert detection_jobs(conn) == {}   # 검출할 키워드는 사용자가 화면에서 고른다


def test_사이트는_키워드를_입력_순서대로_처리한다(make_scheduler, web, conn):
    fake_pexels(web, {})
    repo.add_keywords(conn, ["c", "a", "b"], ["pexels"], None)

    asyncio.run(make_scheduler("pexels").run_until_idle())

    assert searched_keywords(web, "api.pexels.com") == ["c", "a", "b"]


def test_한_사이트만_끝난_키워드는_아직_검출하지_않는다(make_scheduler, web, conn):
    fake_pexels(web, {})
    repo.add_keywords(conn, ["beach"], ["pexels", "pixabay"], None)

    asyncio.run(make_scheduler("pexels").run_until_idle())   # pixabay 는 돌리지 않음

    assert jobs(conn)[("beach", "pexels")]["status"] == "done"
    assert keyword_status(conn) == {"beach": "collecting"}
    assert detection_jobs(conn) == {}


# ── 에러 대응 ───────────────────────────────────────────────────────


def test_심각한_에러는_10분_쉬었다가_커서부터_다시_한다(make_scheduler, web, conn, clock):
    broken = {"on": True}

    def pexels(request):
        if page_of(request) == 2 and broken["on"]:
            return httpx.Response(503)
        photos = [{"id": page_of(request), "src": {"large2x": f"https://img.test/p{page_of(request)}"}}]
        return httpx.Response(200, json={"total_results": 2, "photos": photos,
                                         **({"next_page": "y"} if page_of(request) == 1 else {})})
    web.route("api.pexels.com", pexels)
    web.images({"/p1": make_image(1), "/p2": make_image(2)})
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    started = clock.now()
    seen = {}

    def on_sleep(seconds):
        if seconds > 500:                       # 10분 대기에 들어가는 순간
            seen.update(jobs(conn)[("beach", "pexels")])
            broken["on"] = False                # 쉬는 동안 서버가 복구됨
    clock.on_sleep = on_sleep

    asyncio.run(make_scheduler("pexels").run_until_idle())

    assert seen["status"] == "paused" and seen["cursor"] == "2" and "503" in seen["last_error"]
    assert 590 <= (from_db(seen["resume_at"]) - started).total_seconds() <= 610

    job = jobs(conn)[("beach", "pexels")]
    assert job["status"] == "done" and job["downloaded"] == 2
    # 재개할 때 1페이지를 다시 검색하지 않았다
    assert [page_of(request) for request in web.requests if request.url.host == "api.pexels.com"] == [1, 2, 2, 2]


def test_인증_오류는_3번째에_실패_처리하고_키워드는_끝난_것으로_본다(make_scheduler, web, conn, clock):
    web.route("api.pexels.com", lambda request: httpx.Response(401, text="Unauthorized"))
    fake_pixabay(web, {1: [7]})
    web.images({"/x7": make_image(1)})
    repo.add_keywords(conn, ["beach"], ["pexels", "pixabay"], None)

    asyncio.run(make_scheduler("pexels", "pixabay").run_until_idle())

    pexels = jobs(conn)[("beach", "pexels")]
    assert pexels["status"] == "failed" and pexels["auth_failures"] == 3
    assert "401" in pexels["last_error"]
    assert len([seconds for seconds in clock.sleeps if seconds > 3000]) == 2   # 1시간 대기 2번 후 3번째에 포기
    assert web.count("api.pexels.com") == 3

    # 실패한 사이트가 있어도 나머지가 끝났으므로 수집 완료로 넘어간다
    assert jobs(conn)[("beach", "pixabay")]["status"] == "done"
    assert keyword_status(conn) == {"beach": "collected"}


def test_한_작업이_쉬는_동안_다음_키워드로_넘어간다(make_scheduler, web, conn):
    def pexels(request):
        if request.url.params["query"] == "beach" and len(searched_keywords(web, "api.pexels.com")) <= 2:
            return httpx.Response(503)          # beach 의 첫 시도(자동 재시도 1회 포함)만 실패
        return httpx.Response(200, json={"total_results": 0, "photos": []})
    web.route("api.pexels.com", pexels)
    repo.add_keywords(conn, ["beach", "sunset"], ["pexels"], None)

    asyncio.run(make_scheduler("pexels").run_until_idle())

    # beach 실패(재시도 1회 포함 2번) → 기다리지 않고 sunset → 10분 뒤 beach
    assert searched_keywords(web, "api.pexels.com") == ["beach", "beach", "sunset", "beach"]
    assert {job["status"] for job in jobs(conn).values()} == {"done"}


def test_API_한도에_닿으면_rate_limited로_기다렸다가_이어서_한다(make_scheduler, web, conn, clock):
    fake_unsplash(web, {1: [f"u{i}" for i in range(60)]})          # 다운로드 알림까지 치면 한도 48회를 넘는다
    web.images({f"/uu{i}": make_image(i) for i in range(60)})
    repo.add_keywords(conn, ["beach"], ["unsplash"], None)
    seen = {}
    clock.on_sleep = lambda seconds: seen.update(jobs(conn)[("beach", "unsplash")]) if seconds > 3000 else None

    asyncio.run(make_scheduler("unsplash").run_until_idle())

    assert seen["status"] == "rate_limited" and seen["resume_at"] is not None
    job = jobs(conn)[("beach", "unsplash")]
    assert job["status"] == "done" and job["downloaded"] == 60


# ── 재시작 · 재등록 ─────────────────────────────────────────────────


def test_죽기_전에_돌던_작업은_다시_대기_상태가_된다(conn):
    repo.add_keywords(conn, ["beach"], ["pexels", "pixabay", "naver"], None)
    conn.execute("UPDATE collect_jobs SET status = 'running' WHERE site = 'pexels'")
    conn.execute("UPDATE collect_jobs SET status = 'rate_limited', resume_at = '2026-01-01 00:00:00' WHERE site = 'pixabay'")
    conn.execute("UPDATE collect_jobs SET status = 'paused', resume_at = '2026-01-01 00:00:00' WHERE site = 'naver'")

    assert repo.recover_interrupted(conn) == 2

    statuses = {site: job["status"] for (_, site), job in jobs(conn).items()}
    assert statuses == {"pexels": "pending", "pixabay": "pending", "naver": "paused"}   # 쉬는 작업은 약속한 시각까지 쉰다


def test_중단하면_작업이_pending으로_돌아가고_다시_시작하면_이어진다(make_scheduler, web, conn):
    fake_pexels(web, {1: [1], 2: [2]})
    web.images({"/p1": make_image(1), "/p2": make_image(2)})
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    scheduler = make_scheduler("pexels")

    async def scenario():
        hold = asyncio.Event()
        original = scheduler.deps.sleep

        async def pause_after_first_image(seconds):
            hold.set()                 # 첫 이미지를 저장하고 쉬는 순간
            await asyncio.sleep(3600)  # 여기서 중단될 때까지 멈춰 있는다
        scheduler.deps.sleep = pause_after_first_image

        scheduler.start()
        await hold.wait()
        await scheduler.stop()
        stopped = jobs(conn)[("beach", "pexels")]

        scheduler.deps.sleep = original
        await scheduler.run_until_idle()
        return stopped

    stopped = asyncio.run(scenario())

    assert stopped["status"] == "pending" and stopped["downloaded"] == 1
    job = jobs(conn)[("beach", "pexels")]
    assert job["status"] == "done" and job["downloaded"] == 2


def test_검출을_마친_키워드를_다시_수집하면_검출도_다시_열린다(make_scheduler, web, conn):
    pages = {1: [1]}
    fake_pexels(web, pages)
    web.images({"/p1": make_image(1), "/p2": make_image(2)})
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    asyncio.run(make_scheduler("pexels").run_until_idle())
    repo.select_for_detection(conn, conn.execute("SELECT id FROM keywords").fetchone()[0])
    conn.execute("UPDATE detection_jobs SET status = 'done'")
    conn.execute("UPDATE images SET detected_at = '2026-01-01 00:00:00'")
    conn.commit()

    pages[1] = [2, 1]                                   # 그사이 새 사진이 올라왔다
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    assert keyword_status(conn) == {"beach": "pending"}
    assert detection_jobs(conn)["beach"]["status"] == "waiting"      # 수집하는 동안 새 이미지를 이어서 검출
    asyncio.run(make_scheduler("pexels").run_until_idle())

    job = jobs(conn)[("beach", "pexels")]
    assert (job["status"], job["downloaded"]) == ("done", 1)   # 새 사진 1장만 받음
    assert detection_jobs(conn)["beach"]["status"] == "pending"      # 수집이 끝나면 남은 것을 마무리
    keyword_id = conn.execute("SELECT id FROM keywords").fetchone()[0]
    assert repo.next_undetected_image(conn, keyword_id) is not None  # 새 사진 1장이 검출 대상


def test_진행_중인_키워드를_다시_등록해도_작업을_초기화하지_않는다(conn):
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    conn.execute("UPDATE collect_jobs SET status = 'paused', cursor = '7', downloaded = 100")
    repo.add_keywords(conn, ["beach"], ["pexels", "pixabay"], None)

    assert jobs(conn)[("beach", "pexels")]["cursor"] == "7"
    assert jobs(conn)[("beach", "pixabay")]["status"] == "pending"   # 새 사이트만 추가됨
