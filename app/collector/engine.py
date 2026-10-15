"""수집 엔진 — 키워드×사이트 작업 하나를 처음(또는 저장된 커서)부터 끝까지 돌린다.

Node 버전 api/collector.js 의 페이지 루프·아이템 루프를 옮기고, 세 가지를 더했다.
  1. 커서를 DB 에 저장한다        → 멈췄다 켜도 그 페이지부터 이어서 한다
  2. 중복을 3단계로 거른다        → L0 출처 ID / L1 SHA-256 / L2 지각 해시
  3. 진행 상황을 DB 에 적는다     → 화면이 진행률을 조회할 수 있다

    run_job
      └─ 페이지 루프: 검색 → 아이템 루프 → 커서 저장
            └─ _process_item: L0 → 다운로드 → 검사·해시 → _store(L1 → L2 → 저장)
            └─ 새로 저장했으면: 카운터 올림 → after_save → 잠깐 쉼

작업이 끝나면 그냥 반환하고, 문제가 생기면 예외를 올린다.
그 예외를 보고 '얼마나 쉴지 / 실패 처리할지' 정하는 것은 스케줄러(scheduler.py)의 일이다.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..db import repo
from ..timeutil import to_db
from .dedup import PhashIndex
from .http import AuthFailure, Http, HttpError, NetworkError, RateLimiter, SkipError, Sleep
from .images import ImageInfo, inspect_image, relative_path, save_image
from .sources import Source

log = logging.getLogger(__name__)

# 이미지 다운로드가 이만큼 연달아 실패하면 네트워크나 사이트에 문제가 있다고 보고 작업을 멈춘다.
# (그대로 두면 남은 검색 결과를 전부 '실패'로 흘려보내게 된다)
MAX_CONSECUTIVE_ERRORS = 5


@dataclass
class Deps:
    """엔진이 쓰는 외부 자원 묶음. 테스트에서는 sleep·clock 을 가짜로 바꿔 넣는다."""
    conn: sqlite3.Connection
    settings: Settings
    http: Http
    phash: PhashIndex
    sleep: Sleep
    clock: Callable[[], datetime]


async def run_job(job_id: int, source: Source, limiter: RateLimiter, deps: Deps) -> None:
    conn = deps.conn
    job = repo.get_job(conn, job_id)
    keyword, target = job["keyword"], job["target_count"]
    downloaded = job["downloaded"]
    consecutive_errors = 0

    async def take_api_slot() -> None:
        """API 호출 1회분을 얻는다. 한도에 닿아 기다리는 동안은 상태를 rate_limited 로 바꿔 둔다."""
        def on_wait(seconds: float) -> None:
            resume_at = deps.clock() + timedelta(seconds=seconds)
            repo.set_status(conn, job_id, "rate_limited", resume_at=to_db(resume_at))
            log.info("[%s] API 한도 도달 — %s 대기", source.name,
                     f"{seconds:.0f}초" if seconds < 120 else f"{seconds / 60:.0f}분")

        await limiter.take(on_wait, lambda: repo.set_status(conn, job_id, "running"))

    # 저장된 커서가 있으면 거기서부터, 없으면 첫 페이지부터
    cursor = job["cursor"] or source.first_cursor
    log.info("[%s] '%s' 수집 시작 (커서 %s)", source.name, keyword, cursor)

    while cursor is not None:
        # ── 검색 페이지 1개 ──────────────────────────────────────
        await take_api_slot()
        try:
            data = await source.fetch_page(cursor, keyword, deps.http)
        except HttpError as err:
            if source.is_auth_error(err):
                raise AuthFailure(f"{err} {err.body}".strip()) from err
            if source.is_end_of_results(err, cursor):
                break
            raise

        page = source.parse_page(data, cursor)
        if not page.items:
            break
        # 결과가 끝난 뒤의 빈 페이지는 전체 수를 0 으로 알려 주기도 해서(Pexels), 결과가 있는 페이지만 믿는다
        if page.total is not None:
            repo.set_total_available(conn, job_id, page.total)

        # ── 그 페이지의 아이템들 ─────────────────────────────────
        for item in page.items:
            if target is not None and downloaded >= target:
                log.info("[%s] '%s' 목표 %d장 도달", source.name, keyword, target)
                return  # 커서는 올리지 않는다. 다시 돌려도 L0 가 걸러 준다

            outcome = await _process_item(job, source, item, deps)
            if outcome:
                repo.bump(conn, job_id, outcome)

            # 카운터를 먼저 올리고 나서 쉰다. 쉬는 도중 중단돼도 저장한 장수가 틀어지지 않는다.
            if outcome == "downloaded":
                downloaded += 1
                await source.after_save(item, deps.http, take_api_slot)
                if source.item_delay:
                    await deps.sleep(source.item_delay)

            consecutive_errors = consecutive_errors + 1 if outcome == "errors" else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise NetworkError(f"이미지 다운로드가 {MAX_CONSECUTIVE_ERRORS}장 연속 실패")

        # 페이지를 다 처리한 뒤에만 책갈피를 옮긴다.
        # 도중에 죽으면 이 페이지를 다시 훑게 되지만, 이미 받은 사진은 L0 에서 걸러진다.
        cursor = page.next_cursor
        repo.save_cursor(conn, job_id, cursor)
        if cursor is not None and source.page_delay:
            await deps.sleep(source.page_delay)

    repo.settle_total(conn, job_id)
    log.info("[%s] '%s' 검색 결과를 끝까지 확인", source.name, keyword)


async def _process_item(job: sqlite3.Row, source: Source, item: Any, deps: Deps) -> str | None:
    """아이템 하나를 처리하고 올릴 카운터 이름을 돌려준다.

    'downloaded' 새로 저장 / 'duplicates' 중복 / 'skipped' 건너뜀 / 'errors' 실패 / None 이미 처리한 것
    """
    conn, site, keyword_id = deps.conn, source.name, job["keyword_id"]
    source_id = source.item_id(item)
    url = source.image_url(item)

    # ── L0: 이 사이트의 이 ID 를 본 적 있나? (다운로드 전이라 비용이 없다) ──
    known_image = repo.find_image_by_source(conn, site, source_id)
    if known_image is not None:
        # 다른 키워드에서 받았던 사진이면 "이 키워드에서도 나왔다"만 기록한다.
        # 같은 키워드에서 이미 기록했다면(페이지를 다시 훑는 중) 아무것도 세지 않는다.
        is_new_link = repo.link_source(conn, known_image, keyword_id, site, source_id, url)
        return "duplicates" if is_new_link else None

    if not url:
        return "skipped"

    # ── 다운로드 + 검사 ──────────────────────────────────────────
    try:
        response = await deps.http.get(
            url, headers=source.image_headers, retries=source.image_retries,
            timeout=source.image_timeout, label=site,
        )
        data = response.content
        # 디코딩·해시는 CPU 작업이라 스레드에서 돌린다 (그동안 다른 사이트의 수집이 멈추지 않게)
        info = await asyncio.to_thread(inspect_image, data, source.min_bytes)
    except SkipError as err:
        log.info("[%s] 건너뜀 %s: %s", site, source_id, err)
        return "skipped"
    except HttpError as err:
        if source.is_skippable(err):
            log.info("[%s] 건너뜀 %s: %s", site, source_id, err)
            return "skipped"
        log.warning("[%s] 실패 %s: %s", site, source_id, err)
        return "errors"
    except NetworkError as err:
        log.warning("[%s] 실패 %s: %s", site, source_id, err)
        return "errors"

    return _store(job, source, source_id, url, data, info, deps)


def _store(job: sqlite3.Row, source: Source, source_id: str, url: str, data: bytes,
           info: ImageInfo, deps: Deps) -> str:
    """L1·L2 중복 검사 후 저장한다.

    이 함수에는 await 가 없다. 그래서 '검사 → 저장'이 한 덩어리로 실행되고,
    두 사이트가 같은 사진을 동시에 받아도 둘 다 저장되는 일이 생기지 않는다.
    """
    conn, site, keyword_id = deps.conn, source.name, job["keyword_id"]

    # ── L1: 바이트가 완전히 같은 파일 ────────────────────────────
    same_file = repo.find_image_by_sha(conn, info.sha256)
    if same_file is not None:
        repo.link_source(conn, same_file, keyword_id, site, source_id, url)
        return "duplicates"

    common = dict(sha256=info.sha256, phash=info.phash, width=info.width, height=info.height, size=info.size)

    # ── L2: 리사이즈·재압축된 같은 사진 ──────────────────────────
    # 파일은 저장하지 않고 기록만 남긴다. 임계값이 틀렸다고 판단되면 source_url 로 다시 받을 수 있다.
    similar = deps.phash.find(info.phash)
    if similar is not None:
        image_id = repo.insert_image(conn, path=None, duplicate_of=similar, **common)
        repo.link_source(conn, image_id, keyword_id, site, source_id, url)
        log.info("[%s] 중복(비슷한 이미지) %s → 이미지 #%d", site, source_id, similar)
        return "duplicates"

    # ── 새 이미지 ────────────────────────────────────────────────
    path = relative_path(job["keyword"], site, source_id, info.extension)
    save_image(deps.settings.base_save_path, path, data)
    image_id = repo.insert_image(conn, path=path, duplicate_of=None, **common)
    repo.link_source(conn, image_id, keyword_id, site, source_id, url)
    deps.phash.add(info.phash, image_id)
    log.info("[%s] 저장 %s", site, path)
    return "downloaded"
