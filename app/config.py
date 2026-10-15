"""설정 로드. 값은 모두 .env (또는 환경변수)에서 읽는다."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_DOWNLOADS = 2000


def _flag(value: str | None, fallback: bool) -> bool:
    """true/1/yes/on → True, false/0/no/off → False, 그 밖(비어 있음 등)은 fallback."""
    text = (value or "").strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _positive_int(value: str | None, fallback: int | None) -> int | None:
    """비어 있거나 숫자가 아니거나 0 이하면 fallback."""
    try:
        number = int(value or "")
    except ValueError:
        return fallback
    return number if number > 0 else fallback


@dataclass(frozen=True)
class Settings:
    base_save_path: Path            # 이미지 저장 폴더. 그 아래 {키워드}/{사이트}/ 로 나뉜다
    db_path: Path
    target_count: int | None        # 키워드×사이트 작업 하나에서 받을 최대 장수 (.env 의 MAX_DOWNLOADS, 기본 2000)
    timeout: float                  # 요청 1건당 타임아웃(초)
    max_retries: int                # 네트워크 오류 재시도 횟수
    phash_threshold: int            # 지각 해시가 이 비트 수 이하로 다르면 같은 이미지로 본다
    pause_seconds: int = 600        # 심각한 에러 후 쉬는 시간 (10분)
    auth_pause_seconds: int = 3600  # 인증 오류 후 쉬는 시간 (1시간). 키 문제는 금방 풀리지 않아 길게 둔다
    max_auth_failures: int = 3      # 인증 오류가 이만큼 쌓이면 작업을 실패 처리
    # ── 검출 워커 ──
    model_dir: Path = PROJECT_ROOT / "models"          # yolo/, swinv2/, dinov2/ 가 들어 있는 폴더
    detect_save_crops: bool = True                     # 양성 검출의 Crop·원본 사본·metadata 저장
    detect_save_path: Path = PROJECT_ROOT / "detect"   # 그 아래 YYYYMMDD_dataset/ 를 만든다
    detect_device: str = "auto"                        # 백본 실행 장치: auto | cuda | cpu
    env: Mapping[str, str] = field(default_factory=dict, repr=False)  # 사이트 명세가 자기 키를 꺼내 쓴다

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        if env is None:
            load_dotenv(PROJECT_ROOT / ".env")
            env = os.environ

        return cls(
            base_save_path=Path(env.get("BASE_SAVE_PATH") or PROJECT_ROOT / "Image"),
            db_path=Path(env.get("DB_PATH") or PROJECT_ROOT / "data" / "pipeline.db"),
            target_count=_positive_int(env.get("MAX_DOWNLOADS"), DEFAULT_MAX_DOWNLOADS),
            timeout=_positive_int(env.get("REQUEST_TIMEOUT_MS"), 60_000) / 1000,
            max_retries=_positive_int(env.get("MAX_RETRIES"), 4),
            phash_threshold=_positive_int(env.get("PHASH_THRESHOLD"), 5),
            model_dir=Path(env.get("MODEL_DIR") or PROJECT_ROOT / "models"),
            detect_save_crops=_flag(env.get("DETECT_SAVE_CROPS"), True),
            detect_save_path=Path(env.get("DETECT_SAVE_PATH") or PROJECT_ROOT / "detect"),
            detect_device=(env.get("DETECT_DEVICE") or "auto").strip().lower(),
            env=dict(env),
        )
