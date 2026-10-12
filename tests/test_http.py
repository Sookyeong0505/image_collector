import asyncio

import httpx
import pytest

from app.collector.http import Http, HttpError, RateLimiter, NetworkError, redact_url


def test_URL의_API_키를_가린다():
    assert redact_url("https://pixabay.com/api/?key=abcd&q=cat") == "https://pixabay.com/api/?key=***&q=cat"
    assert "abcd" not in redact_url("https://x.test/?API_KEY=abcd&client_secret=abcd")
    assert redact_url("https://x.test/a?page=2") == "https://x.test/a?page=2"


def test_HttpError_메시지에_키가_남지_않는다():
    err = HttpError(400, "https://pixabay.com/api/?key=SECRET&page=1", "bad")
    assert "SECRET" not in str(err)
    assert err.status == 400 and err.body == "bad"


def test_5xx는_재시도하고_성공하면_응답을_돌려준다(web, http, clock):
    answers = iter([httpx.Response(503), httpx.Response(200, text="ok")])
    web.route("x.test", lambda request: next(answers))

    response = asyncio.run(http.get("https://x.test/"))

    assert response.text == "ok"
    assert web.count("x.test") == 2
    assert len(clock.sleeps) == 1 and 1 <= clock.sleeps[0] <= 1.5   # 첫 백오프는 1초 + 지터


def test_4xx는_재시도하지_않는다(web, http, clock):
    web.route("x.test", lambda request: httpx.Response(404, text="없음"))

    with pytest.raises(HttpError) as caught:
        asyncio.run(http.get("https://x.test/"))

    assert caught.value.status == 404 and caught.value.body == "없음"
    assert web.count("x.test") == 1 and clock.sleeps == []


def test_429는_Retry_After_만큼_기다린다(web, http, clock):
    answers = iter([httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200)])
    web.route("x.test", lambda request: next(answers))

    asyncio.run(http.get("https://x.test/"))

    assert clock.sleeps == [7]


def test_Retry_After가_너무_길면_기다리지_않고_에러를_올린다(web, http, clock):
    web.route("x.test", lambda request: httpx.Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(HttpError):
        asyncio.run(http.get("https://x.test/"))

    assert clock.sleeps == []


def test_재시도를_다_쓰면_NetworkError이고_키는_가려진다(web, http):
    def refuse(request):
        raise httpx.ConnectError("연결 실패")
    web.route("x.test", refuse)

    with pytest.raises(NetworkError) as caught:
        asyncio.run(http.get("https://x.test/?key=SECRET"))

    assert web.count("x.test") == 2          # 최초 1회 + 재시도 1회
    assert "SECRET" not in str(caught.value)


def test_한도에_닿으면_남은_시간만_기다린다(clock):
    limiter = RateLimiter(2, clock=clock.monotonic, sleep=clock.sleep)
    waited, resumed = [], []

    async def scenario():
        await limiter.take()
        await clock.sleep(1000)                      # 창이 열린 지 1000초 지남
        await limiter.take()
        await limiter.take(waited.append, lambda: resumed.append(True))   # 3번째 → 대기

    asyncio.run(scenario())

    assert waited == [3600 - 1000 + 5]               # 1시간 전체가 아니라 남은 2600초 + 여유 5초
    assert resumed == [True]


def test_한도가_없으면_기다리지_않는다(clock):
    limiter = RateLimiter(None, clock=clock.monotonic, sleep=clock.sleep)

    async def scenario():
        for _ in range(1000):
            await limiter.take()

    asyncio.run(scenario())
    assert clock.sleeps == []
