"""수집기 단일 실행 잠금과 시작 시 정리."""

import asyncio
import subprocess
import sys
import time

import httpx
import pytest

from app.collector.images import remove_partial_files
from app.db import repo
from app.instance_lock import AlreadyRunning, InstanceLock
from app.runtime import open_runtime


def test_잠금은_한_번에_하나만_얻을_수_있다(tmp_path):
    first, second = InstanceLock(tmp_path / "db.lock"), InstanceLock(tmp_path / "db.lock")
    first.acquire()

    with pytest.raises(AlreadyRunning):
        second.acquire()

    first.release()
    second.acquire()      # 풀린 뒤에는 얻을 수 있다
    second.release()


def test_프로세스가_강제_종료되면_잠금이_저절로_풀린다(tmp_path):
    lock_path = tmp_path / "db.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from pathlib import Path; from app.instance_lock import InstanceLock;"
         "lock = InstanceLock(Path(sys.argv[1])); lock.acquire(); print('locked', flush=True); time.sleep(60)",
         str(lock_path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning):
            InstanceLock(lock_path).acquire()      # 다른 프로세스가 쥐고 있는 동안은 거부
    finally:
        holder.kill()                              # 정리할 틈 없이 강제 종료
        holder.wait()

    # 잠금 파일은 남아 있지만 다시 얻을 수 있다.
    # Windows 는 죽은 프로세스의 잠금을 곧바로가 아니라 잠시 뒤에 풀 때가 있어 2초까지 다시 시도한다.
    lock = InstanceLock(lock_path)
    for _ in range(20):
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            time.sleep(0.1)
    else:
        lock.acquire()                             # 2초가 지나도 안 풀리면 실패로 보여 준다
    lock.release()


def test_수집기가_도는_중에는_두_번째_수집기를_열_수_없다(settings):
    async def scenario():
        async with open_runtime(settings, httpx.MockTransport(lambda request: httpx.Response(404))):
            with pytest.raises(AlreadyRunning):
                async with open_runtime(settings):
                    pass

    asyncio.run(scenario())


def test_들여다보기만_하는_실행은_수집_중인_작업을_건드리지_않는다(settings, conn):
    """status / check 가 '돌던 작업을 대기로 되돌리는' 복구를 실행하면 안 된다."""
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    conn.execute("UPDATE collect_jobs SET status = 'running'")
    conn.commit()
    part = settings.base_save_path / "beach" / "pexels" / "beach_1.jpg.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"writing...")

    async def scenario():
        async with open_runtime(settings):                            # 수집기가 도는 중
            conn.execute("UPDATE collect_jobs SET status = 'running'")   # (수집기가 작업을 잡은 상태를 흉내)
            conn.commit()
            part.write_bytes(b"writing...")
            async with open_runtime(settings, collector=False) as viewer:   # 그 와중에 status 실행
                return viewer.conn.execute("SELECT status FROM collect_jobs").fetchone()[0], part.exists()

    assert asyncio.run(scenario()) == ("running", True)


def test_수집기를_열면_끊긴_작업과_임시_파일을_정리한다(settings, conn):
    repo.add_keywords(conn, ["beach"], ["pexels"], None)
    conn.execute("UPDATE collect_jobs SET status = 'running'")
    conn.commit()
    folder = settings.base_save_path / "beach" / "pexels"
    folder.mkdir(parents=True)
    (folder / "beach_1.jpg").write_bytes(b"done")
    (folder / "beach_2.jpg.part").write_bytes(b"half")

    async def scenario():
        async with open_runtime(settings):
            pass

    asyncio.run(scenario())

    assert conn.execute("SELECT status FROM collect_jobs").fetchone()[0] == "pending"
    assert sorted(path.name for path in folder.iterdir()) == ["beach_1.jpg"]   # 완성된 이미지는 그대로


def test_임시_파일_정리는_하위_폴더까지_훑고_없는_폴더도_견딘다(tmp_path):
    (tmp_path / "a" / "x").mkdir(parents=True)
    (tmp_path / "a" / "x" / "1.jpg.part").write_bytes(b"")
    (tmp_path / "a" / "2.png.part").write_bytes(b"")
    (tmp_path / "a" / "keep.jpg").write_bytes(b"")

    assert remove_partial_files(tmp_path) == 2
    assert remove_partial_files(tmp_path / "없는폴더") == 0
    assert (tmp_path / "a" / "keep.jpg").exists()
