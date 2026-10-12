"""L2 중복 검사: 지각 해시가 비슷한 이미지를 찾는다.

'비슷하다'는 UNIQUE 제약으로 표현할 수 없어서, 저장된 원본들의 해시를 메모리에 올려 두고
새 이미지가 올 때마다 하나씩 비교한다. 해시는 장당 8바이트라 10만 장이어도 1MB 가 안 된다.
"""

import sqlite3

from ..db import repo
from .images import hamming


class PhashIndex:
    def __init__(self, threshold: int):
        self.threshold = threshold
        self._items: list[tuple[int, int]] = []  # (해시 정수, 이미지 ID)

    @classmethod
    def load(cls, conn: sqlite3.Connection, threshold: int) -> "PhashIndex":
        index = cls(threshold)
        for phash, image_id in repo.load_phashes(conn):
            index.add(phash, image_id)
        return index

    def add(self, phash: str, image_id: int) -> None:
        self._items.append((int(phash, 16), image_id))

    def find(self, phash: str) -> int | None:
        """임계값 이내에서 가장 가까운 이미지의 ID. 없으면 None."""
        target = int(phash, 16)
        best_id, best_distance = None, self.threshold + 1
        for value, image_id in self._items:
            distance = hamming(target, value)
            if distance < best_distance:
                best_id, best_distance = image_id, distance
                if distance == 0:
                    break
        return best_id

    def __len__(self) -> int:
        return len(self._items)
