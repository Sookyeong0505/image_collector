"""수집 엔진: 키워드×사이트 작업 하나가 어떻게 처리되는지."""

import asyncio

import httpx
import pytest

from app.collector.dedup import PhashIndex
from app.collector.engine import Deps, run_job
from app.collector.http import AuthFailure, RateLimiter, NetworkError
from app.collector.sources import build_sources
from app.db import repo
from tests.conftest import fake_naver, fake_pexels, fake_pixabay, fake_unsplash, make_image


@pytest.fixture
def run(conn, settings, http, clock):
    """run('pexels', 'beach') → 그 작업을 끝까지 돌리고 작업 행을 돌려준다."""
    sources = build_sources(settings)
    deps = Deps(conn, settings, http, PhashIndex.load(conn, settings.phash_threshold), clock.sleep, clock.now)

    def _run(site: str, keyword: str = "beach", target: int | None = None, cursor: str | None = None):
        repo.add_keywords(conn, [keyword], [site], target)
        job = conn.execute(
            "SELECT j.id FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id WHERE k.text = ? AND j.site = ?",
            (keyword, site),
        ).fetchone()
        if cursor:
            repo.save_cursor(conn, job["id"], cursor)
        limiter = RateLimiter(None, clock=clock.monotonic, sleep=clock.sleep)
        asyncio.run(run_job(job["id"], sources[site], limiter, deps))
        return repo.get_job(conn, job["id"])

    return _run


def counters(job) -> tuple:
    return job["downloaded"], job["duplicates"], job["skipped"], job["errors"]


def saved_files(settings) -> list[str]:
    base = settings.base_save_path
    return sorted(path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file())


def count(conn, table: str, where: str = "1") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# ── 기본 흐름 ───────────────────────────────────────────────────────


def test_여러_페이지를_끝까지_수집한다(run, web, conn, settings):
    fake_pexels(web, {1: [1, 2], 2: [3]})
    web.images({"/p1": make_image(1), "/p2": make_image(2), "/p3": make_image(3)})

    job = run("pexels")

    assert counters(job) == (3, 0, 0, 0)
    assert job["total_available"] == 3 and job["cursor"] is None
    assert saved_files(settings) == [f"beach/pexels/beach_{i}.jpg" for i in (1, 2, 3)]
    assert count(conn, "images") == 3 and count(conn, "image_sources") == 3


def test_저장된_커서부터_이어서_한다(run, web):
    fake_pexels(web, {1: [1], 2: [2], 3: [3]})
    web.images({"/p2": make_image(2), "/p3": make_image(3)})

    job = run("pexels", cursor="2")

    assert counters(job) == (2, 0, 0, 0)
    assert [path for path in web.paths("api.pexels.com") if "page=1&" in path] == []   # 1페이지는 요청하지 않음


def test_목표_장수에서_멈춘다(run, web, settings):
    fake_pexels(web, {1: [1, 2, 3]})
    web.images({f"/p{i}": make_image(i) for i in (1, 2, 3)})

    job = run("pexels", target=2)

    assert job["downloaded"] == 2 and len(saved_files(settings)) == 2


def test_깨진_이미지와_주소_없는_사진은_건너뛴다(run, web):
    fake_pexels(web, {1: [1, 2]})
    web.images({"/p1": b"<html>error</html>" * 300})   # p2 는 404

    job = run("pexels")

    assert counters(job) == (0, 0, 1, 1)   # p1 은 이미지가 아님(건너뜀), p2 는 다운로드 실패


# ── 중복 제거 ───────────────────────────────────────────────────────


def test_L0_다시_돌려도_이미_받은_사진은_내려받지_않는다(run, web, conn):
    fake_pexels(web, {1: [1, 2]})
    web.images({"/p1": make_image(1), "/p2": make_image(2)})

    run("pexels")
    downloads_before = web.count("img.test")
    conn.execute("UPDATE collect_jobs SET status = 'done'")   # 끝난 작업을 다시 등록하면 처음부터 돈다
    job = run("pexels")

    assert web.count("img.test") == downloads_before          # 이미지 요청이 늘지 않았다
    assert counters(job) == (0, 0, 0, 0)                      # 중복으로도 세지 않는다 (이미 기록된 것)


def test_L0_다른_키워드에서_같은_사진이_나오면_연결만_기록한다(run, web, conn, settings):
    fake_pexels(web, {1: [1]})
    web.images({"/p1": make_image(1)})

    run("pexels", "beach")
    job = run("pexels", "sunset")

    assert counters(job) == (0, 1, 0, 0)
    assert web.count("img.test") == 1                         # 두 번째 키워드에서는 내려받지 않음
    assert saved_files(settings) == ["beach/pexels/beach_1.jpg"]
    assert count(conn, "images") == 1 and count(conn, "image_sources") == 2


def test_L1_다른_사이트의_같은_파일은_한_번만_저장한다(run, web, conn, settings):
    same = make_image(1)
    fake_pexels(web, {1: [1]})
    fake_pixabay(web, {1: [7]})
    web.images({"/p1": same, "/x7": same})

    run("pexels")
    job = run("pixabay")

    assert counters(job) == (0, 1, 0, 0)
    assert saved_files(settings) == ["beach/pexels/beach_1.jpg"]
    assert count(conn, "images") == 1 and count(conn, "image_sources") == 2


def test_L2_리사이즈된_같은_사진은_저장하지_않고_기록만_남긴다(run, web, conn, settings):
    fake_pexels(web, {1: [1]})
    fake_naver(web, ["https://img.test/blog/repost.jpg"])
    web.images({"/p1": make_image(1, size=512), "/blog/repost.jpg": make_image(1, size=200, quality=60)})

    run("pexels")
    job = run("naver")

    assert counters(job) == (0, 1, 0, 0)
    assert saved_files(settings) == ["beach/pexels/beach_1.jpg"]
    record = conn.execute("SELECT * FROM images WHERE path IS NULL").fetchone()
    original = conn.execute("SELECT id FROM images WHERE path IS NOT NULL").fetchone()
    assert record["duplicate_of"] == original["id"]
    # 임계값이 틀렸을 때 다시 받을 수 있도록 원래 주소가 남아 있다
    assert count(conn, "image_sources", "source_url = 'https://img.test/blog/repost.jpg'") == 1


def test_L2_다른_사진은_둘_다_저장한다(run, web, settings):
    fake_pexels(web, {1: [1, 2]})
    web.images({"/p1": make_image(1), "/p2": make_image(2)})

    assert counters(run("pexels")) == (2, 0, 0, 0)


# ── 에러 처리 ───────────────────────────────────────────────────────


def test_다운로드가_연달아_실패하면_작업을_멈춘다(run, web, conn):
    fake_pexels(web, {1: list(range(1, 20))})
    web.route("img.test", lambda request: httpx.Response(500))

    with pytest.raises(NetworkError, match="연속 실패"):
        run("pexels")

    job = conn.execute("SELECT * FROM collect_jobs").fetchone()
    assert job["errors"] == 5 and job["cursor"] is None   # 커서를 올리지 않았으므로 재개하면 이 페이지부터 다시


def test_검색_API의_인증_오류는_AuthFailure(run, web):
    web.route("api.pexels.com", lambda request: httpx.Response(401, text="Unauthorized"))

    with pytest.raises(AuthFailure):
        run("pexels")


def test_Pixabay_잘못된_키는_400이어도_인증_오류로_본다(run, web):
    web.route("pixabay.com", lambda request: httpx.Response(400, text="[ERROR 400] Invalid or missing API key"))

    with pytest.raises(AuthFailure) as caught:
        run("pixabay")

    assert "pixabay-secret" not in str(caught.value)


def test_Pixabay_페이지_범위_초과_400은_정상_종료(run, web):
    fake_pixabay(web, {1: [1]}, total_hits=9999)   # 2페이지가 있다고 하지만 요청하면 400
    web.images({"/x1": make_image(1)})

    assert counters(run("pixabay")) == (1, 0, 0, 0)


def test_Unsplash_한도_초과_403은_인증_오류가_아니다(run, web):
    web.route("api.unsplash.com", lambda request: httpx.Response(403, text="Rate Limit Exceeded"))

    with pytest.raises(Exception) as caught:
        run("unsplash")

    assert not isinstance(caught.value, AuthFailure)


def test_네이버_핫링크_차단은_실패가_아닌_건너뜀(run, web):
    fake_naver(web, ["https://blog.test/a.jpg"])
    web.route("blog.test", lambda request: httpx.Response(403))

    assert counters(run("naver")) == (0, 0, 1, 0)


# ── 사이트별 특수 동작 ──────────────────────────────────────────────


def test_Unsplash는_새로_저장한_사진에만_다운로드_알림을_보낸다(run, web):
    fake_unsplash(web, {1: ["a", "b"]})
    web.images({"/ua": make_image(1), "/ub": make_image(1)})   # b 는 a 와 같은 파일 → 중복

    job = run("unsplash")

    assert counters(job) == (1, 1, 0, 0)
    assert [path for path in web.paths("api.unsplash.com") if "/download" in path] == ["/photos/a/download"]


def test_네이버는_이미지_요청에_Referer를_붙인다(run, web):
    fake_naver(web, ["https://img.test/n.jpg"])
    web.images({"/n.jpg": make_image(1)})

    run("naver")

    image_request = next(request for request in web.requests if request.url.host == "img.test")
    assert image_request.headers["referer"] == "https://search.naver.com/"


def test_네이버_전체_수는_API로_닿을_수_있는_1099장까지만_센다(settings):
    from app.collector.sources.naver import Naver
    naver = Naver(settings)

    assert naver.parse_page({"total": 5_000_000, "items": []}, "1").total == 1099
    assert naver.parse_page({"total": 300, "items": []}, "1").total == 300
