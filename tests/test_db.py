"""스키마가 설계한 규칙을 실제로 강제하는지 확인한다."""

import sqlite3

import pytest

from app.db import connect, init_db

SHA_A = "a" * 64
SHA_B = "b" * 64
PHASH = "0" * 16


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    init_db(connection)
    yield connection
    connection.close()


def add_keyword(conn, text="beach", order=1):
    return conn.execute(
        "INSERT INTO keywords (text, sort_order) VALUES (?, ?)", (text, order)
    ).lastrowid


def add_image(conn, sha=SHA_A, path="Image/beach/pexels/beach_1.jpg", duplicate_of=None):
    return conn.execute(
        "INSERT INTO images (sha256, phash, path, duplicate_of) VALUES (?, ?, ?, ?)",
        (sha, PHASH, path, duplicate_of),
    ).lastrowid


def test_테이블이_모두_만들어진다(conn):
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert tables == {"keywords", "collect_jobs", "images", "image_sources", "detection_jobs"}


def test_init_db는_여러_번_호출해도_데이터를_지우지_않는다(conn):
    add_keyword(conn)
    conn.commit()
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1


def test_연결_설정이_적용된다(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_새_작업의_기본값(conn):
    keyword_id = add_keyword(conn)
    conn.execute("INSERT INTO collect_jobs (keyword_id, site) VALUES (?, 'pexels')", (keyword_id,))
    job = conn.execute("SELECT * FROM collect_jobs").fetchone()

    assert job["status"] == "pending"
    assert job["cursor"] is None
    assert (job["downloaded"], job["duplicates"], job["auth_failures"]) == (0, 0, 0)


def test_같은_키워드와_사이트로_작업을_두_번_만들_수_없다(conn):
    keyword_id = add_keyword(conn)
    conn.execute("INSERT INTO collect_jobs (keyword_id, site) VALUES (?, 'pexels')", (keyword_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO collect_jobs (keyword_id, site) VALUES (?, 'pexels')", (keyword_id,))


def test_정해지지_않은_상태값은_거부한다(conn):
    keyword_id = add_keyword(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO collect_jobs (keyword_id, site, status) VALUES (?, 'pexels', 'sleeping')",
            (keyword_id,),
        )


def test_없는_키워드를_가리키는_작업은_만들_수_없다(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO collect_jobs (keyword_id, site) VALUES (999, 'pexels')")


def test_같은_파일은_한_번만_저장된다(conn):
    """L1 중복 제거: 두 사이트가 동시에 같은 파일을 받아도 DB 가 두 번째를 거부한다."""
    add_image(conn, sha=SHA_A)
    with pytest.raises(sqlite3.IntegrityError):
        add_image(conn, sha=SHA_A, path="Image/beach/pixabay/beach_2.jpg")


def test_지각_해시_중복은_파일_없이_원본을_가리킨다(conn):
    """L2 중복 기록: path 는 비우고 duplicate_of 로 원본을 가리킨다."""
    original = add_image(conn, sha=SHA_A)
    add_image(conn, sha=SHA_B, path=None, duplicate_of=original)

    # 파일도 없고 원본도 없는 행, 파일이 있는데 중복이라는 행은 모순이므로 거부
    with pytest.raises(sqlite3.IntegrityError):
        add_image(conn, sha="c" * 64, path=None, duplicate_of=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_image(conn, sha="d" * 64, path="x.jpg", duplicate_of=original)


def test_같은_사진을_다른_키워드에서_다시_발견하면_연결만_추가된다(conn):
    beach = add_keyword(conn, "beach", 1)
    sunset = add_keyword(conn, "sunset", 2)
    image_id = add_image(conn)

    insert = "INSERT INTO image_sources (image_id, keyword_id, site, source_id) VALUES (?, ?, 'pexels', '555')"
    conn.execute(insert, (image_id, beach))
    conn.execute(insert, (image_id, sunset))

    # 같은 키워드에서 같은 사진을 두 번 기록할 수는 없다
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (image_id, beach))

    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM image_sources").fetchone()[0] == 2


def test_키워드당_검출_작업은_하나만_등록된다(conn):
    keyword_id = add_keyword(conn)
    conn.execute("INSERT INTO detection_jobs (keyword_id) VALUES (?)", (keyword_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO detection_jobs (keyword_id) VALUES (?)", (keyword_id,))


def test_두_연결이_동시에_읽고_쓸_수_있다(tmp_path):
    """수집기가 쓰는 도중에도 검출 워커가 읽을 수 있어야 한다 (WAL 모드)."""
    writer = connect(tmp_path / "shared.db")
    init_db(writer)
    reader = connect(tmp_path / "shared.db")

    add_keyword(writer, "beach")          # 아직 커밋하지 않은 쓰기 트랜잭션이 열려 있다
    assert reader.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 0   # 막히지 않고 읽힌다

    writer.commit()
    assert reader.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1

    writer.close()
    reader.close()


def test_보류_컬럼이_없는_옛_DB를_열면_컬럼을_붙인다(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, sort_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO keywords (text, sort_order) VALUES ('beach', 1);
        PRAGMA user_version = 1;
    """)
    old.close()

    conn = connect(path)
    init_db(conn)
    init_db(conn)                               # 두 번 열어도 안전하다

    row = conn.execute("SELECT text, held FROM keywords").fetchone()
    assert (row["text"], row["held"]) == ("beach", 0)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
