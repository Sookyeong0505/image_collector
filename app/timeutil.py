"""시각 처리. DB 에는 UTC 문자열('2026-10-12 08:30:00')로 저장한다.

이 형식은 문자열로 비교해도 시간 순서와 같아서 SQL 에서 `resume_at <= ?` 로 바로 쓸 수 있다.
"""

from datetime import datetime, timezone

_FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_FORMAT)


def from_db(text: str) -> datetime:
    return datetime.strptime(text, _FORMAT).replace(tzinfo=timezone.utc)
