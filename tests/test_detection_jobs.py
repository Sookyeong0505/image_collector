"""자동 검출의 키워드 지정 · 켜기 · 동작 시간 (명세 5-1, 5-2). torch 없이 돈다."""

import dataclasses
import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db, repo
from app.detector.supervisor import EXIT_LOCKED, DetectorSupervisor, in_window, parse_hhmm
from app.main import create_app

NOW = "2026-10-15 10:00:00"


def add_keyword(conn, text="beach", status="collected") -> int:
    [keyword_id] = repo.add_keywords(conn, [text], ["pexels"], None)
    conn.execute("UPDATE keywords SET status = ? WHERE id = ?", (status, keyword_id))
    conn.commit()
    return keyword_id


def add_image(conn, keyword_id, name, *, path=True, site="pexels") -> int:
    image_id = repo.insert_image(conn, sha256=f"sha-{name}", phash="0" * 16, path=f"/img/{name}.jpg" if path else None,
                                 duplicate_of=None if path else 1, width=10, height=10, size=100)
    repo.link_source(conn, image_id, keyword_id, site, name, None)
    return image_id


def job(conn, keyword_id) -> sqlite3.Row:
    return conn.execute("SELECT * FROM detection_jobs WHERE keyword_id = ?", (keyword_id,)).fetchone()


# ── 키워드 지정과 작업 상태 ─────────────────────────────────────────


def test_고른_키워드만_검출_작업이_된다(conn):
    beach, sunset = add_keyword(conn, "beach"), add_keyword(conn, "sunset")
    add_image(conn, beach, "a")
    add_image(conn, sunset, "b")
    assert not repo.has_detection_work(conn)

    repo.select_for_detection(conn, sunset)
    repo.select_for_detection(conn, sunset)                 # 두 번 골라도 하나
    assert repo.next_detection_job(conn)["keyword_id"] == sunset
    assert repo.next_undetected_image(conn, sunset)["path"] == "/img/b.jpg"

    assert repo.unselect_for_detection(conn, sunset) is True
    assert repo.unselect_for_detection(conn, sunset) is False
    assert not repo.has_detection_work(conn)


def test_남은_이미지가_없으면_수집_완료면_done_수집_중이면_waiting(conn):
    done_keyword, collecting = add_keyword(conn, "a"), add_keyword(conn, "b", status="collecting")
    for keyword_id in (done_keyword, collecting):
        repo.select_for_detection(conn, keyword_id)
        assert repo.settle_detection_job(conn, job(conn, keyword_id)["id"], NOW) in ("done", "waiting")
    assert (job(conn, done_keyword)["status"], job(conn, done_keyword)["finished_at"]) == ("done", NOW)
    assert (job(conn, collecting)["status"], job(conn, collecting)["finished_at"]) == ("waiting", None)


def test_기다리던_작업은_새_이미지가_들어오면_다시_할_일이_된다(conn):
    keyword_id = add_keyword(conn, status="collecting")
    repo.select_for_detection(conn, keyword_id)
    repo.settle_detection_job(conn, job(conn, keyword_id)["id"], NOW)
    assert not repo.has_detection_work(conn)

    add_image(conn, keyword_id, "new")
    assert repo.next_detection_job(conn)["keyword_id"] == keyword_id
    add_image(conn, keyword_id, "dup", path=False)           # 저장하지 않은 중복은 검출하지 않는다
    assert repo.next_undetected_image(conn, keyword_id)["path"] == "/img/new.jpg"


def test_처리_중_끊긴_작업은_다시_pending(conn):
    keyword_id = add_keyword(conn)
    repo.select_for_detection(conn, keyword_id)
    repo.mark_detection_running(conn, job(conn, keyword_id)["id"], NOW)
    assert repo.recover_detection(conn) == 1
    assert job(conn, keyword_id)["status"] == "pending"


def test_검출_결과는_덮어쓰고_detected_at을_채운다(conn):
    keyword_id = add_keyword(conn)
    image_id = add_image(conn, keyword_id, "a")
    detection = {"det_index": 0, "class_id": 1, "yolo_conf": 0.7, "x1": 1, "y1": 2, "x2": 3, "y2": 4,
                 "swinv2_score": 0.1, "dinov2_x15_score": 0.2, "dinov2_x2_score": 0.3,
                 "reasons": '["yolo_confidence"]', "positive": 1, "crop_group": "abcd1234"}
    check = {"pipeline": "original", "verdict": "positive", "section": "full", "yolo_boxes": 1, "skin_passed": 1}
    repo.save_image_check(conn, image_id, check, [detection, {**detection, "det_index": 1}], NOW)
    repo.save_image_check(conn, image_id, {**check, "verdict": "negative"}, [], "2026-10-15 11:00:00")

    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 0
    assert conn.execute("SELECT verdict FROM image_checks").fetchone()[0] == "negative"
    assert conn.execute("SELECT detected_at FROM images").fetchone()[0] == "2026-10-15 11:00:00"
    assert repo.next_undetected_image(conn, keyword_id) is None


def test_검출_현황은_이미지_수와_양성_수를_센다(conn):
    beach, other = add_keyword(conn, "beach"), add_keyword(conn, "other")
    images = [add_image(conn, beach, name) for name in "abc"]
    repo.select_for_detection(conn, beach)
    repo.save_image_check(conn, images[0], {"pipeline": "original", "verdict": "positive"}, [], NOW)
    repo.save_image_check(conn, images[1], {"pipeline": "original", "verdict": "error"}, [], NOW)

    first, second = repo.detection_overview(conn)
    assert (first["keyword"], first["selected"], first["job_status"]) == ("beach", True, "pending")
    assert (first["images"], first["detected"], first["positive"], first["errors"]) == (3, 2, 1, 1)
    assert (second["keyword"], second["selected"], second["images"]) == ("other", False, 0)


def test_클래스별_양성_검출_수를_센다(conn):
    beach, sunset = add_keyword(conn, "beach"), add_keyword(conn, "sunset")
    shared = add_image(conn, beach, "a")
    repo.link_source(conn, shared, sunset, "pexels", "a", None)            # 두 키워드에 걸린 이미지
    other = add_image(conn, beach, "b")
    row = {"yolo_conf": 0.7, "x1": 0, "y1": 0, "x2": 1, "y2": 1, "swinv2_score": 0, "dinov2_x15_score": 0,
           "dinov2_x2_score": 0, "reasons": "[]"}
    check = {"pipeline": "original", "verdict": "positive"}
    repo.save_image_check(conn, shared, check, [
        {**row, "det_index": 0, "class_id": 1, "positive": 1},
        {**row, "det_index": 1, "class_id": 3, "positive": 1},
        {**row, "det_index": 2, "class_id": 3, "positive": 0},            # 음성 검출은 세지 않는다
    ], NOW)
    repo.save_image_check(conn, other, check, [{**row, "det_index": 0, "class_id": 3, "positive": 1}], NOW)
    repo.select_for_detection(conn, beach)
    repo.select_for_detection(conn, sunset)

    overview = {k["keyword"]: k["classes"] for k in repo.detection_overview(conn)}
    assert overview["beach"] == {"1": 1, "3": 2, "4": 0, "5": 0}
    assert overview["sunset"] == {"1": 1, "3": 1, "4": 0, "5": 0}
    assert repo.detection_class_totals(conn) == {"1": 1, "3": 2, "4": 0, "5": 0}   # 같은 이미지는 한 번만


def test_키워드를_숨기고_다시_보이게_한다(conn):
    keyword_id = add_keyword(conn)
    repo.set_detect_hidden(conn, keyword_id, True)
    assert repo.detection_overview(conn)[0]["hidden"] is True
    repo.set_detect_hidden(conn, keyword_id, False)
    assert repo.detection_overview(conn)[0]["hidden"] is False


def test_버전4_DB에는_숨김_컬럼을_붙인다(tmp_path):
    path = tmp_path / "v4.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE keywords (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, sort_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', held INTEGER NOT NULL DEFAULT 0, created_at TEXT);
        INSERT INTO keywords (id, text, sort_order) VALUES (1, 'beach', 1);
        PRAGMA user_version = 4;
    """)
    old.close()
    conn = connect(path)
    init_db(conn)
    assert conn.execute("SELECT detect_hidden FROM keywords").fetchone()[0] == 0
    conn.close()


def test_버전3_DB의_자동_등록_검출_작업은_지운다(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE keywords (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, sort_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', held INTEGER NOT NULL DEFAULT 0, created_at TEXT);
        CREATE TABLE detection_jobs (id INTEGER PRIMARY KEY, keyword_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending', processed INTEGER NOT NULL DEFAULT 0, total INTEGER,
            created_at TEXT, started_at TEXT, finished_at TEXT);
        INSERT INTO keywords (id, text, sort_order, status) VALUES (1, 'beach', 1, 'collected');
        INSERT INTO detection_jobs (keyword_id, total) VALUES (1, 30);
        PRAGMA user_version = 3;
    """)
    old.close()

    conn = connect(path)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM detection_jobs").fetchone()[0] == 0
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(detection_jobs)")}
    assert "total" not in columns
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1   # 키워드는 그대로
    conn.close()


# ── 동작 시간 ───────────────────────────────────────────────────────


def at(hhmm: str) -> datetime:
    return datetime.combine(datetime(2026, 10, 15), parse_hhmm(hhmm))


@pytest.mark.parametrize("moment, expected", [
    ("18:59", False), ("19:00", True), ("23:59", True), ("00:00", True), ("07:59", True), ("08:00", False),
])
def test_자정을_넘는_동작_시간(moment, expected):
    assert in_window(at(moment).time(), "19:00", "08:00") is expected


def test_같은_날_안의_동작_시간과_하루_종일():
    assert in_window(at("12:30").time(), "12:00", "13:00")
    assert not in_window(at("13:00").time(), "12:00", "13:00")
    assert in_window(at("03:00").time(), "00:00", "00:00")       # 시작 = 끝 → 하루 종일


@pytest.mark.parametrize("text", ["7:00", "24:00", "19:60", "", "1900"])
def test_시각_형식이_틀리면_에러(text):
    with pytest.raises(ValueError):
        parse_hhmm(text)


# ── 워커 관리 (수집기 안) ───────────────────────────────────────────


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class Clock:
    def __init__(self, hhmm):
        self.moment = at(hhmm)

    def __call__(self):
        return self.moment


@pytest.fixture
def supervisor(conn):
    started = []

    def spawn():
        started.append(FakeProcess())
        return started[-1]

    clock = Clock("20:00")
    sup = DetectorSupervisor(conn, spawn=spawn, now=clock)
    sup.started, sup.clock = started, clock
    return sup


def ready(conn):
    keyword_id = add_keyword(conn)
    add_image(conn, keyword_id, "a")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)


def test_켜져_있고_동작_시간이고_할_일이_있을_때만_워커를_띄운다(conn, supervisor):
    keyword_id = add_keyword(conn)
    add_image(conn, keyword_id, "a")
    supervisor.check()
    assert supervisor.started == [] and supervisor.status()["state"] == "off"   # 꺼짐

    repo.update_detector_settings(conn, enabled=True)
    supervisor.check()
    assert supervisor.started == [] and supervisor.status()["state"] == "idle"  # 고른 키워드 없음

    repo.select_for_detection(conn, keyword_id)
    supervisor.clock.moment = at("10:00")
    supervisor.check()
    assert supervisor.started == [] and supervisor.status()["state"] == "outside"   # 근무 시간

    supervisor.clock.moment = at("19:00")
    supervisor.check()
    assert len(supervisor.started) == 1 and supervisor.status()["state"] == "running"
    supervisor.check()
    assert len(supervisor.started) == 1                     # 도는 동안 또 띄우지 않는다


def test_워커가_비정상_종료하면_10분_뒤에_다시_띄운다(conn, supervisor):
    ready(conn)
    supervisor.check()
    supervisor.started[0].code = 1
    supervisor.check()
    assert len(supervisor.started) == 1
    status = supervisor.status()
    assert status["state"] == "retry" and status["last_exit"]["code"] == 1 and status["retry_after"] == "20:10"

    supervisor.clock.moment += timedelta(minutes=10)
    supervisor.check()
    assert len(supervisor.started) == 2


def test_다시_켜면_재시작_대기를_푼다(conn, supervisor):
    ready(conn)
    supervisor.check()
    supervisor.started[0].code = 1
    supervisor.check()
    supervisor.clear_retry()
    supervisor.check()
    assert len(supervisor.started) == 2


def test_정상_종료나_다른_워커_실행_중은_바로_다시_띄울_수_있다(conn, supervisor):
    ready(conn)
    supervisor.check()
    supervisor.started[0].code = EXIT_LOCKED
    supervisor.check()
    assert len(supervisor.started) == 2 and supervisor.last_exit is None


# ── API ────────────────────────────────────────────────────────────


def test_API로_켜고_시간을_정하고_키워드를_지정한다(settings, web):
    spawned = []
    app = create_app(dataclasses.replace(settings), httpx.MockTransport(web), spawn_worker=lambda: spawned.append(1))
    with TestClient(app) as client:
        client.post("/api/collector/stop")
        client.post("/api/jobs", json={"keywords": "beach, sunset", "sites": ["pexels"]})

        state = client.get("/api/detection").json()
        assert (state["enabled"], state["window_start"], state["window_end"]) == (False, "19:00", "08:00")
        assert [k["selected"] for k in state["keywords"]] == [False, False]
        ids = {k["keyword"]: k["id"] for k in state["keywords"]}

        state = client.put("/api/detection/settings", json={"enabled": True, "window_start": "18:30"}).json()
        assert (state["enabled"], state["window_start"], state["window_end"]) == (True, "18:30", "08:00")
        assert client.put("/api/detection/settings", json={"window_end": "8:00"}).status_code == 400

        assert client.post(f"/api/detection/keywords/{ids['sunset']}").json() == {"id": ids["sunset"], "selected": True}
        keywords = client.get("/api/detection").json()["keywords"]
        assert [(k["keyword"], k["selected"]) for k in keywords] == [("sunset", True), ("beach", False)]  # 고른 것 먼저

        assert client.post(f"/api/detection/keywords/{ids['beach']}/hide").json() == {"id": ids["beach"], "hidden": True}
        assert [k["hidden"] for k in client.get("/api/detection").json()["keywords"]] == [False, True]
        assert client.post(f"/api/detection/keywords/{ids['beach']}/unhide").json()["hidden"] is False
        assert client.post("/api/detection/keywords/9999/hide").status_code == 404

        assert client.delete(f"/api/detection/keywords/{ids['sunset']}").status_code == 200
        assert client.delete(f"/api/detection/keywords/{ids['sunset']}").status_code == 404
        assert client.post("/api/detection/keywords/9999").status_code == 404
    assert spawned == []   # 고른 키워드에 이미지가 없으니 워커를 띄울 일이 없었다
