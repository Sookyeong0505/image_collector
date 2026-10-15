"""피부색 필터와 좌표 변환 (명세 R8·R9).

reference/src/App.tsx 의 performSkinAnalysis, filterBySkinAndConvertCoordinates 를 옮겼다.
통과한 박스만 원본 이미지 좌표로 바꿔 2차 검증에 넘긴다.
"""

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .yolo import SectionResult

SKIN_RATIO_THRESHOLD = 0.15


@dataclass(frozen=True)
class Candidate:
    """피부색 필터를 통과한 검출 1건. 좌표는 원본 이미지의 정수 좌표."""
    class_id: int
    label: str
    confidence: float   # 소수 넷째 자리로 반올림한 값. 브라우저가 서버에 이 값을 보냈다
    section: str
    x1: int
    y1: int
    x2: int
    y2: int


def js_round(value: float) -> int:
    """JS Math.round: .5 는 항상 올린다. (파이썬 round 는 짝수 쪽으로 보내서 결과가 다르다)"""
    return math.floor(value + 0.5)


def skin_ratio(pixels: np.ndarray) -> float:
    """[높이, 너비, 3] uint8 RGB 에서 YCbCr 피부색 범위에 드는 픽셀의 비율."""
    if pixels.size == 0:
        return 0.0
    rgb = pixels.reshape(-1, 3).astype(np.float64)
    red, green, blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    cr = 128 + 0.5 * red - 0.4187 * green - 0.0813 * blue
    cb = 128 - 0.1687 * red - 0.3313 * green + 0.5 * blue
    skin = (cr >= 133) & (cr <= 173) & (cb >= 77) & (cb <= 127)
    return float(skin.mean())


def is_skin(pixels: np.ndarray) -> bool:
    """비율이 15% 를 '넘어야' 통과 (같으면 탈락)."""
    return pixels.size > 0 and skin_ratio(pixels) > SKIN_RATIO_THRESHOLD


def filter_by_skin(image: Image.Image, result: SectionResult) -> list[Candidate]:
    """채택한 구역의 박스 중 피부색 필터를 통과한 것만 원본 좌표로 바꿔 돌려준다. image 는 RGB.

    브라우저는 구역을 원래 크기 그대로 Canvas(폭·높이는 올림)에 그린 뒤 박스 영역의 픽셀을 읽었다.
    폭이 홀수인 이미지의 오른쪽 구역은 0.5픽셀 어긋나게 그려지는데, 여기서는 내림한 위치의 픽셀을 쓴다.
    """
    section = result.section
    canvas_width = math.ceil(section.width)
    canvas_height = math.ceil(section.height)
    offset_x = math.floor(section.x)
    offset_y = math.floor(section.y)
    pixels = np.asarray(image)

    candidates = []
    for box in result.boxes:
        x1 = max(0, math.floor(box.x1))
        y1 = max(0, math.floor(box.y1))
        x2 = min(canvas_width, math.ceil(box.x2))
        y2 = min(canvas_height, math.ceil(box.y2))
        if x2 <= x1 or y2 <= y1:
            continue
        region = pixels[offset_y + y1:offset_y + y2, offset_x + x1:offset_x + x2]
        if not is_skin(region):
            continue
        candidates.append(Candidate(
            class_id=box.class_id,
            label=box.label,
            confidence=round(box.confidence, 4),
            section=section.name,
            x1=js_round(x1 + section.x),
            y1=js_round(y1 + section.y),
            x2=js_round(x2 + section.x),
            y2=js_round(y2 + section.y),
        ))
    return candidates
