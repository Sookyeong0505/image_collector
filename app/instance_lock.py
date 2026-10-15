"""수집기가 한 번에 하나만 돌도록 막는 잠금.

수집기가 둘이면 같은 작업을 동시에 처리해 같은 사진을 두 번 받고 진행 상태가 엉킨다.
예: 자동 시작 작업이 도는 중에 `python -m app collect` 를 또 실행하는 경우.

DB 파일 옆의 잠금 파일(pipeline.db.lock)을 운영체제 수준에서 잠근다.
프로세스가 죽으면 운영체제가 잠금을 풀어 주므로, 강제 종료 뒤에 '잠금이 남아서 못 켜는' 일이 없다.
(파일이 있는지만 보는 방식은 강제 종료되면 파일이 남아 다음 실행을 막는다.)
"""

import sys
from pathlib import Path


class AlreadyRunning(Exception):
    """다른 수집기 프로세스가 이미 잠금을 쥐고 있다."""


class InstanceLock:
    def __init__(self, path: Path,
                 busy_message: str = "수집기가 이미 실행 중입니다 (자동 시작 작업 또는 다른 serve / collect / resume)."):
        self.path = path
        self.busy_message = busy_message   # 검출 워커는 자기 잠금 파일과 안내 문구를 쓴다
        self._file = None

    def acquire(self) -> None:
        """잠금을 얻는다. 다른 프로세스가 쥐고 있으면 기다리지 않고 AlreadyRunning 을 낸다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = open(self.path, "a+")
        try:
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            file.close()
            raise AlreadyRunning(self.busy_message) from err
        self._file = file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
