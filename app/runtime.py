"""실행에 필요한 부품을 조립한다. CLI 와 웹 서버가 같은 조립 코드를 쓴다.

    Settings ─┬─ SQLite 연결 (스키마 준비, 죽기 전 작업 복구)
              ├─ httpx 클라이언트 → Http (재시도)
              ├─ 사이트 명세들
              └─ Scheduler
"""

import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from .collector.http import Http, HttpError, NetworkError
from .collector.scheduler import Scheduler
from .collector.sources import Source, build_sources
from .config import Settings
from .db import connect, init_db, repo


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    http: Http
    sources: dict[str, Source]   # 키가 없는 사이트도 포함
    scheduler: Scheduler         # 키가 있는 사이트만 돌린다


@asynccontextmanager
async def open_runtime(settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
    """transport 는 테스트에서 가짜 웹을 끼워 넣을 때만 쓴다."""
    conn = connect(settings.db_path)
    init_db(conn)
    repo.recover_interrupted(conn)

    # 이미지 CDN 은 리다이렉트가 흔하다. 연결 수를 제한해 RAM 과 상대 서버 부담을 줄인다.
    client = httpx.AsyncClient(
        transport=transport, follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
    try:
        http = Http(client, retries=settings.max_retries, timeout=settings.timeout)
        sources = build_sources(settings)
        configured = {name: source for name, source in sources.items() if source.configured}
        yield Runtime(settings, conn, http, sources, Scheduler(conn, settings, configured, http))
    finally:
        await client.aclose()
        conn.close()


async def check_source(source: Source, http: Http) -> tuple[bool, str]:
    """API 연결 확인. (성공 여부, 사람이 읽을 메시지)"""
    if not source.configured:
        return False, f".env 에 {', '.join(source.required_env)} 값이 없습니다"
    try:
        await source.check(http)
    except HttpError as err:
        if source.is_auth_error(err):
            return False, f"API 키가 올바르지 않습니다 (HTTP {err.status})"
        return False, f"API 가 오류를 돌려줬습니다 (HTTP {err.status})"
    except NetworkError as err:
        return False, f"연결할 수 없습니다 ({err})"
    return True, "연결 성공"


def parse_keywords(text: str) -> list[str]:
    """'beach, sunset ,, beach' → ['beach', 'sunset'] (공백 정리, 빈 항목·중복 제거, 순서 유지)"""
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
