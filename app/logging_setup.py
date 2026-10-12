"""로그 설정: 콘솔과 logs/collector.log 양쪽에 남긴다."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PROJECT_ROOT

_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_logging(log_dir: Path = PROJECT_ROOT / "logs") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    # 5MB 가 차면 collector.log.1, .2 … 로 넘기고 5개까지만 보관한다 (밤새 돌려도 디스크를 채우지 않게)
    file_handler = RotatingFileHandler(
        log_dir / "collector.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )

    # Windows 콘솔(cp949)이 표현하지 못하는 문자가 섞여도 로그 때문에 죽지 않게 한다
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:   # pythonw 로 실행하면(자동 시작) 콘솔이 없다. 그때는 파일에만 남긴다
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(errors="replace")
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=logging.INFO, format=_FORMAT, datefmt="%m-%d %H:%M:%S", handlers=handlers, force=True)

    # httpx 는 요청마다 전체 URL 을 INFO 로 찍는다. Pixabay 는 URL 에 API 키가 들어 있으므로 꺼 둔다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
