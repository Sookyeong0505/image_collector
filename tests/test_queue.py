"""예약 키워드 관리: 순서 바꾸기, 보류, 삭제."""

import asyncio
import dataclasses
import time

import httpx
from fastapi.testclient import TestClient

from app.collector.scheduler import Scheduler
from app.collector.sources import build_sources
from app.db import repo
from app.main import create_app
from tests.conftest import fake_pexels, make_image


def make_scheduler(conn, settings, http, clock) -> Scheduler:
    sources = {"pexels": build_sources(settings)["pexels"]}
    return Scheduler(conn, settings, sources, http, clock=clock.now, sleep=clock.sleep, monotonic=clock.monotonic)


def statuses(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT k.text, j.status FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id"
    ).fetchall()
    return {row["text"]: row["status"] for row in rows}


def searched(web) -> list[str]:
    return [request.url.params["query"] for request in web.requests if request.url.host == "api.pexels.com"]


def ids_of(conn) -> dict[str, int]:
    return {row["text"]: row["id"] for row in conn.execute("SELECT id, text FROM keywords")}


def test_순서를_바꾸면_그_순서대로_수집한다(conn, settings, http, clock, web):
    fake_pexels(web, {1: []})
    repo.add_keywords(conn, ["beach", "sunset", "forest"], ["pexels"], None)
    ids = ids_of(conn)

    repo.reorder_keywords(conn, [ids["forest"], ids["beach"], ids["sunset"]])
    asyncio.run(make_scheduler(conn, settings, http, clock).run_until_idle())

    assert searched(web) == ["forest", "beach", "sunset"]


def test_순서를_바꿔도_목록에_없는_키워드의_자리는_그대로다(conn):
    repo.add_keywords(conn, ["a", "b", "c"], ["pexels"], None)
    ids = ids_of(conn)

    repo.reorder_keywords(conn, [ids["c"], ids["a"]])          # b 는 빼고 a, c 만 맞바꾼다

    order = [row["text"] for row in conn.execute("SELECT text FROM keywords ORDER BY sort_order")]
    assert order == ["c", "b", "a"]


def test_보류한_키워드는_건너뛰고_남은_작업으로_세지_않는다(conn, settings, http, clock, web):
    fake_pexels(web, {1: []})
    repo.add_keywords(conn, ["beach", "sunset"], ["pexels"], None)
    repo.set_held(conn, ids_of(conn)["beach"], True)

    asyncio.run(make_scheduler(conn, settings, http, clock).run_until_idle())

    assert searched(web) == ["sunset"]
    assert statuses(conn) == {"beach": "pending", "sunset": "done"}
    assert repo.count_unfinished_jobs(conn) == 0                # 보류만 남았으면 PC 를 깨워 둘 필요가 없다

    repo.set_held(conn, ids_of(conn)["beach"], False)
    asyncio.run(make_scheduler(conn, settings, http, clock).run_until_idle())
    assert statuses(conn) == {"beach": "done", "sunset": "done"}


def test_수집_중인_키워드를_보류하면_멈추고_다음_키워드로_넘어간다(conn, settings, http, clock, web):
    pages = {"beach": [1, 2], "sunset": [3]}

    def pexels(request):
        ids = pages[request.url.params["query"]]
        photos = [{"id": i, "src": {"large2x": f"https://img.test/p{i}"}} for i in ids]
        return httpx.Response(200, json={"total_results": len(ids), "photos": photos})
    web.route("api.pexels.com", pexels)
    web.images({f"/p{i}": make_image(i) for i in (1, 2, 3)})
    repo.add_keywords(conn, ["beach", "sunset"], ["pexels"], None)
    beach = ids_of(conn)["beach"]
    scheduler = make_scheduler(conn, settings, http, clock)

    async def scenario():
        saved_one = asyncio.Event()
        original = scheduler.deps.sleep

        async def stall_after_first_image(seconds):
            saved_one.set()
            await asyncio.sleep(3600)          # 보류로 멈출 때까지 여기 머문다
        scheduler.deps.sleep = stall_after_first_image

        scheduler.start()
        await saved_one.wait()
        scheduler.deps.sleep = original
        repo.set_held(conn, beach, True)
        await scheduler.release_keyword(beach)

        for _ in range(500):                   # 다시 뜬 태스크가 sunset 을 끝낼 때까지
            if statuses(conn)["sunset"] == "done":
                break
            await asyncio.sleep(0.01)
        await scheduler.stop()

    asyncio.run(scenario())

    assert statuses(conn) == {"beach": "pending", "sunset": "done"}
    job = conn.execute("SELECT * FROM collect_jobs WHERE keyword_id = ?", (beach,)).fetchone()
    assert job["downloaded"] == 1                # 받은 장수가 남아 있어 보류를 풀면 이어서 한다


def test_삭제하면_수집_기록은_지우고_이미지는_남긴다(conn):
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    keyword_id = ids_of(conn)["beach"]
    image_id = repo.insert_image(conn, sha256="a" * 64, phash="0" * 16, path="beach/pexels/beach_1.jpg",
                                 duplicate_of=None, width=10, height=10, size=100)
    repo.link_source(conn, image_id, keyword_id, "pexels", "1", "https://img.test/p1")

    repo.delete_keyword(conn, keyword_id)

    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM collect_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM image_sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1


def test_API로_순서_보류_삭제를_한다(settings, web):
    app = create_app(dataclasses.replace(settings), httpx.MockTransport(web))
    with TestClient(app) as client:
        client.post("/api/collector/stop")      # 등록한 키워드가 바로 수집되지 않게 멈춰 둔다
        client.post("/api/jobs", json={"keywords": "beach, sunset, forest", "sites": ["pexels"]})
        ids = {k["keyword"]: k["id"] for k in client.get("/api/progress").json()["keywords"]}

        assert client.put("/api/keywords/order", json={"ids": [ids["forest"], ids["beach"], ids["sunset"]]}).status_code == 200
        assert client.post(f"/api/keywords/{ids['beach']}/hold").json() == {"id": ids["beach"], "held": True}
        assert client.delete(f"/api/keywords/{ids['sunset']}").status_code == 200
        keywords = client.get("/api/progress").json()["keywords"]

        assert [(k["keyword"], k["held"]) for k in keywords] == [("forest", False), ("beach", True)]
        assert client.post(f"/api/keywords/{ids['beach']}/resume").json()["held"] is False
        assert client.delete("/api/keywords/9999").status_code == 404


def test_수집을_마친_키워드는_보류하거나_지울_수_없다(settings, web):
    fake_pexels(web, {1: []})
    app = create_app(dataclasses.replace(settings), httpx.MockTransport(web))
    with TestClient(app) as client:
        client.post("/api/jobs", json={"keywords": "beach", "sites": ["pexels"]})
        for _ in range(200):
            keyword = client.get("/api/progress").json()["keywords"][0]
            if keyword["status"] == "collected":
                break
            time.sleep(0.02)

        assert keyword["finished_at"] is not None
        assert client.post(f"/api/keywords/{keyword['id']}/hold").status_code == 409
        assert client.delete(f"/api/keywords/{keyword['id']}").status_code == 409


def test_완료하면_남은_작업을_끝내고_수집_완료로_옮긴다(conn):
    repo.add_keywords(conn, ["beach"], ["pexels", "pixabay", "naver"], None)
    keyword_id = ids_of(conn)["beach"]
    conn.execute("UPDATE collect_jobs SET status = 'done', finished_at = '2026-01-01 00:00:00' WHERE site = 'pexels'")
    conn.execute("UPDATE collect_jobs SET status = 'failed' WHERE site = 'pixabay'")
    conn.execute("UPDATE collect_jobs SET status = 'paused', resume_at = '2026-01-01 01:00:00' WHERE site = 'naver'")
    repo.set_held(conn, keyword_id, True)

    assert repo.finish_keyword(conn, keyword_id, "2026-01-01 00:30:00") is True

    rows = {row["site"]: row for row in conn.execute("SELECT * FROM collect_jobs")}
    assert {site: row["status"] for site, row in rows.items()} == {"pexels": "done", "pixabay": "failed", "naver": "done"}
    assert rows["naver"]["finished_at"] == "2026-01-01 00:30:00" and rows["naver"]["resume_at"] is None
    assert rows["pexels"]["finished_at"] == "2026-01-01 00:00:00"      # 이미 끝난 작업은 건드리지 않는다
    keyword = repo.get_keyword(conn, keyword_id)
    assert (keyword["status"], keyword["held"]) == ("collected", 0)
    assert conn.execute("SELECT COUNT(*) FROM detection_jobs").fetchone()[0] == 0   # 검출할 키워드는 사용자가 고른다


def test_수집_중인_키워드를_API로_완료한다(settings, web):
    app = create_app(dataclasses.replace(settings), httpx.MockTransport(web))
    with TestClient(app) as client:
        client.post("/api/collector/stop")
        client.post("/api/jobs", json={"keywords": "beach", "sites": ["pexels"]})
        keyword_id = client.get("/api/progress").json()["keywords"][0]["id"]

        assert client.post(f"/api/keywords/{keyword_id}/complete").json() == {"id": keyword_id, "status": "collected"}
        keyword = client.get("/api/progress").json()["keywords"][0]

        assert keyword["status"] == "collected" and keyword["finished_at"] is not None
        assert keyword["jobs"][0]["status"] == "done"
        assert client.post(f"/api/keywords/{keyword_id}/complete").status_code == 409   # 이미 완료
