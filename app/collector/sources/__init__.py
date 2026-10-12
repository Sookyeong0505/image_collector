"""사이트 명세 목록. 새 사이트를 추가하면 ALL_SOURCES 에만 넣으면 된다."""

from ...config import Settings
from .base import Page, Source
from .naver import Naver
from .pexels import Pexels
from .pixabay import Pixabay
from .unsplash import Unsplash

ALL_SOURCES: tuple[type[Source], ...] = (Unsplash, Pexels, Pixabay, Naver)


def build_sources(settings: Settings) -> dict[str, Source]:
    """모든 사이트 명세를 만든다 (키가 없는 사이트도 포함 — configured 로 구분한다)."""
    return {cls.name: cls(settings) for cls in ALL_SOURCES}


__all__ = ["ALL_SOURCES", "Page", "Source", "build_sources"]
