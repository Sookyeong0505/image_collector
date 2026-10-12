"""절전 방지 — 수집할 작업이 남아 있는 동안 PC 가 절전 모드로 들어가지 않게 한다.

Windows 의 전원 설정을 바꾸지 않고, "지금 일하는 중"이라고 Windows 에 알리는 방식이다
(동영상 플레이어가 재생 중에 쓰는 것과 같은 기능). 그래서
  - 작업이 끝나면 평소 전원 설정대로 다시 절전에 들어가고
  - 프로그램이 죽으면 요청도 함께 사라진다.

모니터가 꺼지는 것은 막지 않는다. 노트북 덮개를 닫거나 전원 버튼으로 절전시키는 것은 막을 수 없다.

주의: 이 요청은 '호출한 스레드'에 묶인다. 켜고 끄는 호출을 같은 스레드(이벤트 루프)에서 해야 한다.
"""

import logging
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000       # 다시 호출할 때까지 이 상태를 유지
_ES_SYSTEM_REQUIRED = 0x00000001  # 시스템이 절전에 들어가지 않게 함


def _windows_setter() -> Callable[[int], object] | None:
    if sys.platform != "win32":
        return None  # 다른 OS 에서는 아무것도 하지 않는다
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState


class KeepAwake:
    def __init__(self, set_state: Callable[[int], object] | None = None):
        self._set_state = set_state or _windows_setter()
        self.active = False

    def update(self, busy: bool) -> None:
        """busy 가 바뀔 때만 Windows 에 알린다. 여러 번 불러도 된다."""
        if busy == self.active or self._set_state is None:
            return
        self._set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED if busy else _ES_CONTINUOUS)
        self.active = busy
        log.info("절전 방지 %s", "켬 (남은 작업 있음)" if busy else "끔 (남은 작업 없음)")
