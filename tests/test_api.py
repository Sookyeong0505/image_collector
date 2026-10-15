"""웹 API: 가짜 웹을 끼운 서버를 실제로 띄워 등록부터 완료까지 확인한다."""

import dataclasses
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import fake_pexels, make_image


@pytest.fixture
def client(settings, web):
    # naver 키를 비워 '키 없는 사이트'가 어떻게 보이는지도 확인한다
    env = {**settings.env, "NAVER_CLIENT_ID": ""}
    app = create_app(dataclasses.replace(settings, env=env), httpx.MockTransport(web))
    with TestClient(app) as test_client:   # with 블록에 들어갈 때 서버 시작(스케줄러 포함), 나올 때 종료
        yield test_client


def wait_until(condition, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("시간 안에 조건이 충족되지 않음")


def test_사이트_목록은_키_설정_여부를_알려준다(client):
    sites = {site["name"]: site for site in client.get("/api/sites").json()}

    assert set(sites) == {"unsplash", "pexels", "pixabay", "naver"}
    assert sites["pexels"]["configured"] is True and sites["naver"]["configured"] is False


def test_연결_확인(client, web):
    fake_pexels(web, {1: []})
    web.route("api.unsplash.com", lambda request: httpx.Response(401, text="invalid token"))

    assert client.post("/api/sites/pexels/check").json()["ok"] is True
    assert client.post("/api/sites/unsplash/check").json() == {
        "name": "unsplash", "ok": False, "message": "API 키가 올바르지 않습니다 (HTTP 401)"}
    assert client.post("/api/sites/naver/check").json()["ok"] is False      # 키 없음
    assert client.post("/api/sites/nowhere/check").status_code == 404


def test_등록하면_수집이_시작되고_진행률이_보인다(client, web, settings):
    fake_pexels(web, {1: [1, 2]})
    web.images({"/p1": make_image(1), "/p2": make_image(2)})

    answer = client.post("/api/jobs", json={"keywords": " beach, ,beach ", "sites": ["pexels"]})
    assert answer.status_code == 201 and answer.json() == {"keywords": ["beach"], "sites": ["pexels"]}

    def finished():
        data = client.get("/api/progress").json()
        return data if data["keywords"][0]["status"] == "collected" else None
    data = wait_until(finished)

    job = data["keywords"][0]["jobs"][0]
    assert (job["site"], job["status"], job["downloaded"]) == ("pexels", "done", 2)
    assert (settings.base_save_path / "beach" / "pexels" / "beach_1.jpg").exists()


def test_최대_장수는_env_값만_쓰고_진행바_분모는_사이트가_알려준_전체_수다(settings, web):
    fake_pexels(web, {1: [1, 2], 2: [3]})                   # 사이트가 알려 주는 전체 수 = 3
    web.images({f"/p{i}": make_image(i) for i in (1, 2, 3)})
    app = create_app(dataclasses.replace(settings, target_count=2), httpx.MockTransport(web))

    with TestClient(app) as client:
        # 화면에서 장수를 보내도 받지 않는다 (.env 의 MAX_DOWNLOADS 만 쓴다)
        client.post("/api/jobs", json={"keywords": "beach", "sites": ["pexels"], "target_count": 99})

        def finished():
            data = client.get("/api/progress").json()
            return data if data["keywords"][0]["status"] == "collected" else None
        data = wait_until(finished)

    job = data["keywords"][0]["jobs"][0]
    assert data["max_downloads"] == 2
    assert job["target_count"] == 2 and job["downloaded"] == 2
    assert job["total_available"] == 3


@pytest.mark.parametrize("body, reason", [
    ({"keywords": " , ", "sites": ["pexels"]}, "키워드"),
    ({"keywords": "beach", "sites": []}, "사이트"),
    ({"keywords": "beach", "sites": ["naver"]}, "naver"),       # 키가 없는 사이트
    ({"keywords": "beach", "sites": ["flickr"]}, "flickr"),     # 없는 사이트
])
def test_잘못된_등록은_거부한다(client, body, reason):
    answer = client.post("/api/jobs", json=body)

    assert answer.status_code == 400 and reason in answer.json()["detail"]
    assert client.get("/api/progress").json()["keywords"] == []


def test_중단하고_재개할_수_있다(client):
    assert client.get("/api/progress").json()["running"] is True
    assert client.post("/api/collector/stop").json() == {"running": False}
    assert client.get("/api/progress").json()["running"] is False
    assert client.post("/api/collector/start").json() == {"running": True}


def test_상태_확인(client, web):
    fake_pexels(web, {1: [1]})
    web.images({"/p1": make_image(1)})

    idle = client.get("/api/health").json()
    assert idle == {"status": "ok", "scheduler_running": True, "unfinished_jobs": 0,
                    "keep_awake": False, "last_saved_at": None}

    client.post("/api/jobs", json={"keywords": "beach", "sites": ["pexels"]})
    done = wait_until(lambda: (h := client.get("/api/health").json())["last_saved_at"] and h["unfinished_jobs"] == 0 and h)
    assert done["keep_awake"] is False      # 다 끝났으므로 절전 방지 해제


def test_화면이_열린다(client):
    page = client.get("/")
    assert page.status_code == 200 and "이미지 수집기" in page.text
