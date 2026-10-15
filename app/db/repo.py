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
        # 검출을 마친 키워드를 다시 수집하면, 새로 받는 이미지를 이어서 검출하도록 기다리는 상태로 돌린다
        conn.execute("UPDATE detection_jobs SET status = 'waiting', finished_at = NULL "
                     "WHERE keyword_id = ? AND status = 'done'", (keyword_id,))

    conn.commit()
    return keyword_ids


# ── 예약 키워드 관리 (순서 · 보류 · 삭제) ─────────────────────────────


def get_keyword(conn: sqlite3.Connection, keyword_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()


def reorder_keywords(conn: sqlite3.Connection, keyword_ids: list[int]) -> None:
    """예약 키워드를 주어진 순서로 바꾼다.

    이 키워드들이 쓰던 sort_order 값들을 모아 새 순서대로 다시 나눠 준다.
    그래서 목록에 없는 키워드(수집 완료 등)의 자리는 그대로다.
    """
    marks = ",".join("?" * len(keyword_ids))
    orders = sorted(row[0] for row in conn.execute(
        f"SELECT sort_order FROM keywords WHERE id IN ({marks})", keyword_ids))
    for keyword_id, order in zip(keyword_ids, orders):
        conn.execute("UPDATE keywords SET sort_order = ? WHERE id = ?", (order, keyword_id))
    conn.commit()


def set_held(conn: sqlite3.Connection, keyword_id: int, held: bool) -> None:
    conn.execute("UPDATE keywords SET held = ? WHERE id = ?", (int(held), keyword_id))
    conn.commit()


def finish_keyword(conn: sqlite3.Connection, keyword_id: int, now: str) -> bool:
    """남은 사이트 작업을 모두 끝난 것(done)으로 바꾸고 키워드를 수집 완료로 옮긴다.

    사용자가 '이 정도면 됐다'고 판단해 수집을 마칠 때 쓴다. 이미 받은 이미지는 그대로 둔다.
    """
    conn.execute(
        """
        UPDATE collect_jobs SET status = 'done', resume_at = NULL, finished_at = ?,
            last_error = '수동 완료'
        WHERE keyword_id = ? AND status NOT IN ('done', 'failed')
        """,
        (now, keyword_id),
    )
    conn.execute("UPDATE keywords SET held = 0 WHERE id = ?", (keyword_id,))
    conn.commit()
    return complete_keyword_if_finished(conn, keyword_id)


def delete_keyword(conn: sqlite3.Connection, keyword_id: int) -> None:
    """키워드와 그 수집 작업·출처 기록을 지운다 (외래키 CASCADE).

    이미 저장한 이미지 파일과 images 행은 남긴다. 나중에 같은 키워드를 다시 등록하면
    SHA-256 검사가 같은 파일을 알아보고 다시 저장하지 않는다.
    """
    conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    conn.commit()


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
        WHERE j.site = ? AND k.held = 0
          AND (j.status = 'pending' OR (j.status = 'paused' AND j.resume_at <= ?))
        ORDER BY k.sort_order LIMIT 1
        """,
        (site, now),
    ).fetchone()


def earliest_resume(conn: sqlite3.Connection, site: str) -> str | None:
    """쉬고 있는 작업 중 가장 먼저 깨어날 시각. 쉬는 작업이 없으면 None. 보류한 키워드는 빼고 본다."""
    return conn.execute(
        """
        SELECT MIN(j.resume_at) FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id
        WHERE j.site = ? AND j.status = 'paused' AND k.held = 0
        """,
        (site,),
    ).fetchone()[0]


def count_unfinished_jobs(conn: sqlite3.Connection) -> int:
    """아직 끝나지 않은 작업 수 (대기·수집 중·한도 대기·일시 정지). 보류한 키워드는 세지 않는다."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM collect_jobs j JOIN keywords k ON k.id = j.keyword_id
        WHERE j.status NOT IN ('done', 'failed') AND k.held = 0
        """
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
    """키워드의 모든 사이트 작업이 끝났으면(done/failed) 키워드를 수집 완료로 옮긴다.

    실패한 사이트가 있어도 끝난 것으로 본다. 검출할 키워드는 사용자가 고르므로 여기서 등록하지 않는다.
    이미 검출 대상으로 고른 키워드라면 검출 작업을 pending 으로 돌린다. 워커가 남은 이미지를
    검출하고 작업을 done 으로 마무리한다.
    """
    remaining = conn.execute(
        "SELECT COUNT(*) FROM collect_jobs WHERE keyword_id = ? AND status NOT IN ('done', 'failed')",
        (keyword_id,),
    ).fetchone()[0]
    if remaining:
        return False

    conn.execute("UPDATE keywords SET status = 'collected' WHERE id = ?", (keyword_id,))
    conn.execute(
        "UPDATE detection_jobs SET status = 'pending', finished_at = NULL "
        "WHERE keyword_id = ? AND status IN ('waiting', 'done')",
        (keyword_id,),
    )
    conn.commit()
    return True


# ── 수집 엔진용 ─────────────────────────────────────────────────────


def save_cursor(conn: sqlite3.Connection, job_id: int, cursor: str | None) -> None:
    conn.execute("UPDATE collect_jobs SET cursor = ? WHERE id = ?", (cursor, job_id))
    conn.commit()


# 확인한 장수 = 진행바의 분자
_CHECKED = "(downloaded + duplicates + skipped + errors)"


def set_total_available(conn: sqlite3.Connection, job_id: int, total: int) -> None:
    """사이트가 알려 준 전체 수를 적는다. 이미 확인한 수보다 작으면 확인한 수를 쓴다.

    Pixabay 는 totalHits(500)보다 많이(600장) 돌려주기도 한다. 사이트 숫자를 그대로 쓰면 진행바가 넘친다.
    """
    conn.execute(f"UPDATE collect_jobs SET total_available = MAX(?, {_CHECKED}) WHERE id = ?", (total, job_id))
    conn.commit()


def settle_total(conn: sqlite3.Connection, job_id: int) -> None:
    """검색 결과를 끝까지 봤을 때 부른다. 실제로 확인한 수가 이 키워드로 받을 수 있는 전체 수다.

    사이트가 알려 주는 수는 실제와 다르다. Pexels 는 수천 장이라고 하고 480장쯤에서 끝나고,
    네이버도 1099장보다 일찍 끝나는 일이 많다.
    """
    conn.execute(f"UPDATE collect_jobs SET total_available = {_CHECKED} WHERE id = ?", (job_id,))
    conn.commit()


def repair_totals(conn: sqlite3.Connection) -> int:
    """위 규칙이 생기기 전에 쌓인 전체 수를 고친다. 고친 작업 수를 돌려준다.

      - 결과를 끝까지 보고 끝난 작업(done 인데 목표 장수에 못 미침) → 확인한 수
      - 확인한 수가 전체 수보다 큰 작업 → 확인한 수
      - 네이버 전체 수가 API 로 닿을 수 있는 1099장을 넘는 작업 → 1099
    """
    changed = conn.execute(
        f"""
        UPDATE collect_jobs SET total_available = {_CHECKED}
        WHERE total_available IS NOT NULL AND total_available != {_CHECKED}
          AND ((status = 'done' AND (target_count IS NULL OR downloaded < target_count))
               OR {_CHECKED} > total_available)
        """
    ).rowcount
    changed += conn.execute(
        "UPDATE collect_jobs SET total_available = 1099 WHERE site = 'naver' AND total_available > 1099"
    ).rowcount
    conn.commit()
    return changed


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
    keywords = conn.execute(
        """
        SELECT k.*, (SELECT MAX(finished_at) FROM collect_jobs WHERE keyword_id = k.id) AS finished_at
        FROM keywords k ORDER BY k.sort_order
        """
    ).fetchall()
    jobs = conn.execute("SELECT * FROM collect_jobs ORDER BY site").fetchall()

    result = []
    for keyword in keywords:
        result.append({
            "id": keyword["id"],
            "keyword": keyword["text"],
            "status": keyword["status"],
            "held": bool(keyword["held"]),
            "finished_at": keyword["finished_at"] if keyword["status"] == "collected" else None,   # UTC
            "jobs": [
                {name: job[name] for name in (
                    "site", "status", "downloaded", "duplicates", "skipped", "errors",
                    "target_count", "total_available", "auth_failures", "resume_at", "last_error",
                )}
                for job in jobs if job["keyword_id"] == keyword["id"]
            ],
        })
    return result


# ── 검출 (설계: docs/DETECTION_SPEC.md 5장) ─────────────────────────
#
# 검출 작업 1개 = 사용자가 고른 키워드 1개. 진행률은 저장하지 않고 images · image_checks 에서 센다.

# 이 키워드로 받은 이미지 중 아직 검출하지 않은 것 (파일이 있는 원본만)
_UNDETECTED = """
    SELECT i.id, i.path FROM image_sources s JOIN images i ON i.id = s.image_id
    WHERE s.keyword_id = {keyword} AND i.path IS NOT NULL AND i.detected_at IS NULL
"""

# 워커가 할 일이 있는 작업: 처음 등록됐거나 다시 열린 작업(pending), 기다리던 중 새 이미지가 들어온 작업(waiting)
_WORKABLE_JOBS = f"""
    SELECT d.*, k.text AS keyword
    FROM detection_jobs d JOIN keywords k ON k.id = d.keyword_id
    WHERE d.status = 'pending'
       OR (d.status = 'waiting' AND EXISTS ({_UNDETECTED.format(keyword="d.keyword_id")}))
    ORDER BY d.id
"""


def get_detector_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM detector_settings WHERE id = 1").fetchone()
    return {"enabled": bool(row["enabled"]), "window_start": row["window_start"], "window_end": row["window_end"]}


def update_detector_settings(conn: sqlite3.Connection, *, enabled: bool | None = None,
                             window_start: str | None = None, window_end: str | None = None) -> dict:
    """None 인 항목은 그대로 둔다. 시각 형식('HH:MM')은 부르는 쪽에서 검사한다."""
    if enabled is not None:
        conn.execute("UPDATE detector_settings SET enabled = ? WHERE id = 1", (int(enabled),))
    if window_start is not None:
        conn.execute("UPDATE detector_settings SET window_start = ? WHERE id = 1", (window_start,))
    if window_end is not None:
        conn.execute("UPDATE detector_settings SET window_end = ? WHERE id = 1", (window_end,))
    conn.commit()
    return get_detector_settings(conn)


def select_for_detection(conn: sqlite3.Connection, keyword_id: int) -> None:
    """키워드를 검출 대상에 넣는다. 이미 들어 있으면 그대로 둔다."""
    conn.execute("INSERT INTO detection_jobs (keyword_id) VALUES (?) ON CONFLICT (keyword_id) DO NOTHING",
                 (keyword_id,))
    conn.commit()


def unselect_for_detection(conn: sqlite3.Connection, keyword_id: int) -> bool:
    """검출 대상에서 뺀다. 이미 검출한 결과는 남는다. 빠진 게 있으면 True."""
    removed = conn.execute("DELETE FROM detection_jobs WHERE keyword_id = ?", (keyword_id,)).rowcount
    conn.commit()
    return bool(removed)


def set_detect_hidden(conn: sqlite3.Connection, keyword_id: int, hidden: bool) -> None:
    """검출 화면의 '키워드 추가' 목록에서 숨기거나 다시 보이게 한다. 수집과 검출에는 영향이 없다."""
    conn.execute("UPDATE keywords SET detect_hidden = ? WHERE id = ?", (int(hidden), keyword_id))
    conn.commit()


def recover_detection(conn: sqlite3.Connection) -> int:
    """워커가 죽기 전에 처리하던 작업을 pending 으로 돌린다. 워커가 시작할 때 부른다."""
    count = conn.execute("UPDATE detection_jobs SET status = 'pending' WHERE status = 'running'").rowcount
    conn.commit()
    return count


def has_detection_work(conn: sqlite3.Connection) -> bool:
    return conn.execute(f"SELECT EXISTS ({_WORKABLE_JOBS})").fetchone()[0] == 1


def next_detection_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(_WORKABLE_JOBS + " LIMIT 1").fetchone()


def detection_job_exists(conn: sqlite3.Connection, job_id: int) -> bool:
    """사용자가 처리 도중에 키워드를 빼면 행이 사라진다."""
    return conn.execute("SELECT 1 FROM detection_jobs WHERE id = ?", (job_id,)).fetchone() is not None


def mark_detection_running(conn: sqlite3.Connection, job_id: int, now: str) -> None:
    conn.execute("UPDATE detection_jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                 (now, job_id))
    conn.commit()


def release_detection_job(conn: sqlite3.Connection, job_id: int) -> None:
    """워커가 작업을 마치지 못하고 멈출 때 (꺼짐 · 동작 시간 끝). 다음 실행 때 이어서 한다."""
    conn.execute("UPDATE detection_jobs SET status = 'pending' WHERE id = ? AND status = 'running'", (job_id,))
    conn.commit()


def next_undetected_image(conn: sqlite3.Connection, keyword_id: int) -> sqlite3.Row | None:
    return conn.execute(_UNDETECTED.format(keyword="?") + " ORDER BY i.id LIMIT 1", (keyword_id,)).fetchone()


def settle_detection_job(conn: sqlite3.Connection, job_id: int, now: str) -> str:
    """남은 이미지가 없을 때 부른다. 키워드 수집이 끝났으면 done, 아직 수집 중이면 waiting."""
    collected = conn.execute(
        "SELECT k.status = 'collected' FROM detection_jobs d JOIN keywords k ON k.id = d.keyword_id WHERE d.id = ?",
        (job_id,),
    ).fetchone()[0]
    status = "done" if collected else "waiting"
    conn.execute("UPDATE detection_jobs SET status = ?, finished_at = ? WHERE id = ?",
                 (status, now if collected else None, job_id))
    conn.commit()
    return status


def save_image_check(conn: sqlite3.Connection, image_id: int, check: dict, detections: list[dict], now: str) -> None:
    """이미지 1장의 검출 결과를 한 트랜잭션으로 기록한다. 같은 이미지를 다시 검출하면 덮어쓴다.

    check: image_checks 의 컬럼 값 (image_id, checked_at 제외)
    detections: detections 의 컬럼 값 목록 (id, image_id 제외)
    """
    with conn:   # 중간에 예외가 나면 전부 되돌린다
        conn.execute("DELETE FROM detections WHERE image_id = ?", (image_id,))
        columns = ["image_id", *check, "checked_at"]
        conn.execute(
            f"INSERT OR REPLACE INTO image_checks ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [image_id, *check.values(), now],
        )
        for detection in detections:
            columns = ["image_id", *detection]
            conn.execute(
                f"INSERT INTO detections ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [image_id, *detection.values()],
            )
        conn.execute("UPDATE images SET detected_at = ? WHERE id = ?", (now, image_id))


def detection_class_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """검출 대상 키워드 전체의 클래스별 양성 검출 수. 여러 키워드에 걸린 이미지는 한 번만 센다."""
    totals = {str(c): 0 for c in (1, 3, 4, 5)}
    for class_id, count in conn.execute(
        """
        SELECT d.class_id, COUNT(DISTINCT d.id)
        FROM detection_jobs j
        JOIN image_sources s ON s.keyword_id = j.keyword_id
        JOIN detections d ON d.image_id = s.image_id
        WHERE d.positive = 1
        GROUP BY d.class_id
        """
    ):
        totals[str(class_id)] = count
    return totals


def clear_image_checks(conn: sqlite3.Connection, image_ids: list[int]) -> int:
    """검출 결과를 지우고 detected_at 을 비워 다시 검출하게 한다. 지운 이미지 수."""
    if not image_ids:
        return 0
    marks = ",".join("?" * len(image_ids))
    with conn:
        conn.execute(f"DELETE FROM detections WHERE image_id IN ({marks})", image_ids)
        conn.execute(f"DELETE FROM image_checks WHERE image_id IN ({marks})", image_ids)
        return conn.execute(f"UPDATE images SET detected_at = NULL WHERE id IN ({marks})", image_ids).rowcount


def detection_overview(conn: sqlite3.Connection) -> list[dict]:
    """화면용: 모든 키워드의 검출 지정 여부와 진행률. 검출 대상(고른 순서)을 먼저, 나머지는 키워드 순서로."""
    rows = conn.execute(
        """
        SELECT k.id, k.text, k.status AS collect_status, k.detect_hidden,
               d.id AS job_id, d.status AS job_status, d.finished_at,
               COUNT(DISTINCT i.id) AS images,
               COUNT(DISTINCT CASE WHEN i.detected_at IS NOT NULL THEN i.id END) AS detected,
               COUNT(DISTINCT CASE WHEN c.verdict = 'positive' THEN i.id END) AS positive,
               COUNT(DISTINCT CASE WHEN c.verdict = 'error' THEN i.id END) AS errors
        FROM keywords k
        LEFT JOIN detection_jobs d ON d.keyword_id = k.id
        LEFT JOIN image_sources s ON s.keyword_id = k.id
        LEFT JOIN images i ON i.id = s.image_id AND i.path IS NOT NULL
        LEFT JOIN image_checks c ON c.image_id = i.id
        GROUP BY k.id
        ORDER BY d.id IS NULL, d.id, k.sort_order
        """
    ).fetchall()
    # 키워드별·클래스별 양성 검출 수 (= class{N} 폴더에 저장되는 Crop 묶음 수)
    classes: dict[int, dict[str, int]] = {}
    for keyword_id, class_id, count in conn.execute(
        """
        SELECT s.keyword_id, d.class_id, COUNT(DISTINCT d.id)
        FROM image_sources s JOIN detections d ON d.image_id = s.image_id
        WHERE d.positive = 1
        GROUP BY s.keyword_id, d.class_id
        """
    ):
        classes.setdefault(keyword_id, {})[str(class_id)] = count
    return [
        {
            "id": row["id"], "keyword": row["text"], "collect_status": row["collect_status"],
            "classes": {str(c): classes.get(row["id"], {}).get(str(c), 0) for c in (1, 3, 4, 5)},
            "selected": row["job_id"] is not None, "hidden": bool(row["detect_hidden"]),
            "job_status": row["job_status"],
            "finished_at": row["finished_at"],   # UTC
            "images": row["images"], "detected": row["detected"],
            "positive": row["positive"], "errors": row["errors"],
        }
        for row in rows
    ]
