"""절전 방지: 남은 작업이 있을 때만 켜지는지."""

import asyncio

from app.collector.scheduler import Scheduler
from app.collector.sources import build_sources
from app.db import repo
from app.keep_awake import KeepAwake
from tests.conftest import fake_pexels, make_image

AWAKE = 0x80000001   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
NORMAL = 0x80000000  # ES_CONTINUOUS


def test_상태가_바뀔_때만_Windows에_알린다():
    calls = []
    keep_awake = KeepAwake(calls.append)

    for busy in (False, True, True, True, False, False):
        keep_awake.update(busy)

    assert calls == [AWAKE, NORMAL]
    assert keep_awake.active is False


def test_수집하는_동안_켜지고_끝나면_꺼진다(conn, settings, http, clock, web):
    fake_pexels(web, {1: [1]})
    web.images({"/p1": make_image(1)})
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    calls = []
    keep_awake = KeepAwake(calls.append)
    during_download = []
    clock.on_sleep = lambda seconds: during_download.append(keep_awake.active)   # 이미지 저장 직후 쉬는 순간
    sources = {"pexels": build_sources(settings)["pexels"]}
    scheduler = Scheduler(conn, settings, sources, http, clock=clock.now, sleep=clock.sleep,
                          monotonic=clock.monotonic, keep_awake=keep_awake)

    asyncio.run(scheduler.run_until_idle())

    assert during_download == [True]
    assert calls == [AWAKE, NORMAL]


def test_1시간_쉬는_작업이_있으면_그동안_깨어_있다(conn, settings, http, clock, web):
    import httpx
    answers = iter([httpx.Response(503), httpx.Response(503)])   # 첫 시도와 자동 재시도가 실패 → 1시간 정지
    web.route("api.pexels.com", lambda request: next(answers, httpx.Response(200, json={"photos": []})))
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    keep_awake = KeepAwake(lambda state: None)
    while_paused = []
    clock.on_sleep = lambda seconds: while_paused.append(keep_awake.active) if seconds > 3000 else None
    sources = {"pexels": build_sources(settings)["pexels"]}
    scheduler = Scheduler(conn, settings, sources, http, clock=clock.now, sleep=clock.sleep,
                          monotonic=clock.monotonic, keep_awake=keep_awake)

    asyncio.run(scheduler.run_until_idle())

    assert while_paused == [True]          # 쉬는 동안에도 PC 를 깨워 둬야 1시간 뒤에 재개할 수 있다
    assert keep_awake.active is False      # 다 끝나면 해제


def test_실제_Windows_호출이_실패하지_않는다():
    keep_awake = KeepAwake()
    keep_awake.update(True)
    keep_awake.update(False)
    assert keep_awake.active is False
