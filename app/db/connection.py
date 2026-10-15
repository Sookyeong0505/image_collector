"""SQLite 연결과 스키마 초기화.

SQLite 는 서버 없이 파일 하나(data/pipeline.db)에 DB 전체를 담는다.
수집기와 검출 워커, 두 프로세스가 같은 파일을 열기 때문에 아래 PRAGMA 설정이 중요하다.
"""

import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 스키마를 바꿀 때마다 1씩 올린다. DB 파일 안의 PRAGMA user_version 과 비교해
# "옛 스키마로 만든 DB"를 알아챌 수 있다.
SCHEMA_VERSION = 2   # 2: keywords.held (보류) 추가


def default_db_path() -> Path:
    """.env 의 DB_PATH 가 있으면 그 경로, 없으면 프로젝트의 data/pipeline.db."""
    return Path(os.environ.get("DB_PATH") or PROJECT_ROOT / "data" / "pipeline.db")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """DB 연결을 연다. 파일이 없으면 새로 만든다.

    연결은 스레드·프로세스마다 따로 열어야 한다 (하나를 나눠 쓰지 않는다).
    """
    db_path = Path(path) if path else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)

    # 결과를 row["site"] 처럼 컬럼 이름으로 꺼낼 수 있게 한다 (기본은 row[2] 같은 튜플).
    conn.row_factory = sqlite3.Row

    # WAL: 변경 내용을 별도 로그 파일에 먼저 적는 방식.
    # 수집기가 쓰는 동안에도 검출 워커가 읽을 수 있다. 한 번 켜면 DB 파일에 기억된다.
    conn.execute("PRAGMA journal_mode = WAL")

    # 다른 연결이 쓰는 중이면 바로 에러를 내지 않고 5초까지 기다린다.
    conn.execute("PRAGMA busy_timeout = 5000")

    # 외래키 검사. SQLite 는 기본값이 꺼짐이고 연결마다 켜야 한다.
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL 모드에서 권장되는 설정. 프로그램이 죽어도 DB 는 깨지지 않는다.
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """테이블이 없으면 만든다. 여러 번 호출해도 안전하다."""
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"DB 스키마 버전({found})이 이 코드가 아는 버전({SCHEMA_VERSION})보다 높습니다. "
            "더 새로운 코드로 만든 DB 입니다."
        )

    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """옛 버전으로 만든 DB 에 새 컬럼을 붙인다.

    CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블을 건드리지 않으므로, 컬럼 추가는 여기서 따로 한다.
    컬럼이 있는지 보고 붙이므로 여러 번 호출해도 안전하다.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(keywords)")}
    if "held" not in columns:   # 버전 1 → 2
        conn.execute("ALTER TABLE keywords ADD COLUMN held INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0, 1))")
