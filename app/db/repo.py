"""DB 질의 모음. SQL 은 전부 이 파일에 둔다.

수집기는 연결 하나를 이벤트 루프 스레드에서만 쓴다. 그래서 함수 하나가 실행되는 동안에는
다른 수집 태스크가 끼어들 수 없다 (await 가 없으므로).
"""

import sqlite3

# ── 키워드 · 작업 등록 ──────────────────────────────────────────────


def add_keywords(conn: sqlite3.Connection, keywords: list[str], sites: list[str],
                 target_count: int | None) -> list[int]:
    """키워드마다 사이트별 수집 작업을 만든다. 만든(또는 다시 연) 키워드 ID 목록을 돌려준다.

    이미 끝난 키워드를 다시 넣으면 작업을 처음부터 다시 돌린다.
    그동안 새로 올라온 사진만 받게 되고, 이미 받은 사진은 중복 검사가 걸러 준다.
    """
    next_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM keywords").fetchone()[0]
    keyword_ids = []

    for text in keywords:
        row = conn.execute("SELECT id FROM keywords WHERE text = ?", (text,)).fetchone()
        if row:
            keyword_id = row["id"]
        else:
            next_order += 1
            keyword_id = conn.execute(
                "INSERT INTO keywords (text, sort_order) VALUES (?, ?)", (text, next_order)
            ).lastrowid
        keyword_ids.append(keyword_id)

        for site in sites:
            conn.execute(
                """
                INSERT INTO collect_jobs (keyword_id, site, target_count) VALUES (?, ?, ?)
                ON CONFLICT (keyword_id, site) DO UPDATE SET
                    status = 'pending', cursor = NULL, target_count = excluded.target_count,
                    downloaded = 0, duplicates = 0, skipped = 0, errors = 0, auth_failures = 0,
                    resume_at = NULL, last_error = NULL, started_at = NULL, finished_at = NULL
                WHERE collect_jobs.status IN ('done', 'failed')   -- 진행 중인 작업은 건드리지 않는다
                """,
                (keyword_id, site, target_count),
            )
        conn.execute("UPDATE keywords SET status = 'pending' WHERE id = ? AND status = 'collected'", (keyword_id,))

    conn.commit()
    return keyword_ids


# ── 스케줄러용 ──────────────────────────────────────────────────────

_JOB_COLUMNS = "j.*, k.text AS keyword"


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """프로그램이 죽기 전에 돌던 작업을 다시 대기 상태로 돌린다. 시작할 때 한 번 호출한다."""
    count = conn.execute(
        "UPDATE collect_jobs SET status = 'pending', resume_at = NULL WHERE status IN ('running', 'rate_limited')"
    ).rowcount
    conn.commit()
    return count


def next_runnable_job(conn: sqlite3.Connection, site: str, now: str) -> sqlite3.Row | None:
    """이 사이트가 지금 할 수 있는 작업 중 키워드 순서가 가장 빠른 것."""
    return conn.execute(
        f"""
        SELECT {_JOB_COLUMNS} FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id
        WHERE j.site = ?
          AND (j.status = 'pending' OR (j.status = 'paused' AND j.resume_at <= ?))
        ORDER BY k.sort_order LIMIT 1
        """,
        (site, now),
    ).fetchone()


def earliest_resume(conn: sqlite3.Connection, site: str) -> str | None:
    """쉬고 있는 작업 중 가장 먼저 깨어날 시각. 쉬는 작업이 없으면 None."""
    return conn.execute(
        "SELECT MIN(resume_at) FROM collect_jobs WHERE site = ? AND status = 'paused'", (site,)
    ).fetchone()[0]


def count_unfinished_jobs(conn: sqlite3.Connection) -> int:
    """아직 끝나지 않은 작업 수 (대기·수집 중·한도 대기·일시 정지)."""
    return conn.execute(
        "SELECT COUNT(*) FROM collect_jobs WHERE status NOT IN ('done', 'failed')"
    ).fetchone()[0]


def last_saved_at(conn: sqlite3.Connection) -> str | None:
    """마지막으로 이미지를 저장한 시각(UTC). 수집이 실제로 진행 중인지 가늠하는 데 쓴다."""
    return conn.execute("SELECT MAX(created_at) FROM images WHERE path IS NOT NULL").fetchone()[0]


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()


def mark_running(conn: sqlite3.Connection, job_id: int, now: str) -> None:
    conn.execute(
        "UPDATE collect_jobs SET status = 'running', resume_at = NULL, started_at = COALESCE(started_at, ?) WHERE id = ?",
        (now, job_id),
    )
    conn.execute(
        "UPDATE keywords SET status = 'collecting' WHERE id = (SELECT keyword_id FROM collect_jobs WHERE id = ?)",
        (job_id,),
    )
    conn.commit()


def set_status(conn: sqlite3.Connection, job_id: int, status: str, *, resume_at: str | None = None,
               last_error: str | None = None, finished_at: str | None = None) -> None:
    conn.execute(
        """
        UPDATE collect_jobs
        SET status = ?, resume_at = ?, last_error = COALESCE(?, last_error), finished_at = ?
        WHERE id = ?
        """,
        (status, resume_at, last_error, finished_at, job_id),
    )
    conn.commit()


def add_auth_failure(conn: sqlite3.Connection, job_id: int) -> int:
    """인증 오류 횟수를 1 올리고 올린 뒤의 값을 돌려준다."""
    conn.execute("UPDATE collect_jobs SET auth_failures = auth_failures + 1 WHERE id = ?", (job_id,))
    conn.commit()
    return conn.execute("SELECT auth_failures FROM collect_jobs WHERE id = ?", (job_id,)).fetchone()[0]


def complete_keyword_if_finished(conn: sqlite3.Connection, keyword_id: int) -> bool:
    """키워드의 모든 사이트 작업이 끝났으면(done/failed) 검출 작업을 등록한다.

    수집기와 검출 워커를 잇는 유일한 지점이다. 실패한 사이트가 있어도 끝난 것으로 본다.
    """
    remaining = conn.execute(
        "SELECT COUNT(*) FROM collect_jobs WHERE keyword_id = ? AND status NOT IN ('done', 'failed')",
        (keyword_id,),
    ).fetchone()[0]
    if remaining:
        return False

    # 이 키워드로 모은 이미지 중 아직 검출하지 않은 장수
    total = conn.execute(
        """
        SELECT COUNT(DISTINCT i.id) FROM image_sources s JOIN images i ON i.id = s.image_id
        WHERE s.keyword_id = ? AND i.path IS NOT NULL AND i.detected_at IS NULL
        """,
        (keyword_id,),
    ).fetchone()[0]

    conn.execute("UPDATE keywords SET status = 'collected' WHERE id = ?", (keyword_id,))
    conn.execute(
        """
        INSERT INTO detection_jobs (keyword_id, total) VALUES (?, ?)
        ON CONFLICT (keyword_id) DO UPDATE SET     -- 키워드를 다시 수집한 경우: 검출도 다시 대기열에
            status = 'pending', total = excluded.total, processed = 0, started_at = NULL, finished_at = NULL
        """,
        (keyword_id, total),
    )
    conn.commit()
    return True


# ── 수집 엔진용 ─────────────────────────────────────────────────────


def save_cursor(conn: sqlite3.Connection, job_id: int, cursor: str | None) -> None:
    conn.execute("UPDATE collect_jobs SET cursor = ? WHERE id = ?", (cursor, job_id))
    conn.commit()


def set_total_available(conn: sqlite3.Connection, job_id: int, total: int) -> None:
    conn.execute("UPDATE collect_jobs SET total_available = ? WHERE id = ?", (total, job_id))
    conn.commit()


def bump(conn: sqlite3.Connection, job_id: int, counter: str) -> None:
    """진행 카운터 하나를 1 올린다."""
    if counter not in ("downloaded", "duplicates", "skipped", "errors"):
        raise ValueError(counter)
    conn.execute(f"UPDATE collect_jobs SET {counter} = {counter} + 1 WHERE id = ?", (job_id,))
    conn.commit()


def find_image_by_source(conn: sqlite3.Connection, site: str, source_id: str) -> int | None:
    """L0: 이 사이트의 이 사진 ID 를 (어느 키워드에서든) 본 적 있으면 그 이미지 ID."""
    row = conn.execute(
        "SELECT image_id FROM image_sources WHERE site = ? AND source_id = ? LIMIT 1", (site, source_id)
    ).fetchone()
    return row["image_id"] if row else None


def find_image_by_sha(conn: sqlite3.Connection, sha256: str) -> int | None:
    """L1: 바이트가 완전히 같은 파일을 이미 갖고 있으면 그 이미지 ID."""
    row = conn.execute("SELECT id FROM images WHERE sha256 = ?", (sha256,)).fetchone()
    return row["id"] if row else None


def insert_image(conn: sqlite3.Connection, *, sha256: str, phash: str, path: str | None,
                 duplicate_of: int | None, width: int, height: int, size: int) -> int:
    """이미지 행을 추가한다. 커밋은 link_source 까지 끝낸 뒤 호출하는 쪽에서 한다."""
    return conn.execute(
        "INSERT INTO images (sha256, phash, path, duplicate_of, width, height, bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sha256, phash, path, duplicate_of, width, height, size),
    ).lastrowid


def link_source(conn: sqlite3.Connection, image_id: int, keyword_id: int, site: str,
                source_id: str, source_url: str | None) -> bool:
    """'이 이미지를 이 사이트·키워드에서 발견했다'를 기록한다. 이미 기록돼 있었으면 False."""
    added = conn.execute(
        "INSERT OR IGNORE INTO image_sources (image_id, keyword_id, site, source_id, source_url) VALUES (?, ?, ?, ?, ?)",
        (image_id, keyword_id, site, source_id, source_url),
    ).rowcount
    conn.commit()
    return added == 1


def load_phashes(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """저장된 원본 이미지의 (지각 해시, 이미지 ID) 전체. 중복 기록(path 없음)은 뺀다."""
    return [(row["phash"], row["id"]) for row in conn.execute("SELECT phash, id FROM images WHERE path IS NOT NULL")]


# ── 조회용 ──────────────────────────────────────────────────────────


def progress(conn: sqlite3.Connection) -> list[dict]:
    """키워드별 진행 상황. 화면과 CLI 가 그대로 보여 준다."""
    keywords = conn.execute("SELECT * FROM keywords ORDER BY sort_order").fetchall()
    jobs = conn.execute("SELECT * FROM collect_jobs ORDER BY site").fetchall()

    result = []
    for keyword in keywords:
        result.append({
            "id": keyword["id"],
            "keyword": keyword["text"],
            "status": keyword["status"],
            "jobs": [
                {name: job[name] for name in (
                    "site", "status", "downloaded", "duplicates", "skipped", "errors",
                    "target_count", "total_available", "auth_failures", "resume_at", "last_error",
                )}
                for job in jobs if job["keyword_id"] == keyword["id"]
            ],
        })
    return result
