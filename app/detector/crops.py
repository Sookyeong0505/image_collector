"""3배율 Crop과 파일 이름 규칙 (명세 R10, 5-3).

reference/server/app.py 의 crop_detection, safe_stem 과 파일 이름 규칙을 옮겼다.
reference 서버는 파이썬이었으므로 반올림도 파이썬 round 그대로 쓴다.
"""

import re
from pathlib import Path

from PIL import Image

from .skin import Candidate

SCALES = (("x1", 1.0), ("x1.5", 1.5), ("x2", 2.0))
CropBox = tuple[int, int, int, int]


def crop_box(width: int, height: int, candidate: Candidate, scale: float) -> CropBox:
    """박스 중심을 고정하고 가로·세로를 scale 배로 넓힌 영역. 이미지 밖은 경계에서 자른다."""
    x1 = max(0, min(width, round(float(candidate.x1))))
    y1 = max(0, min(height, round(float(candidate.y1))))
    x2 = max(0, min(width, round(float(candidate.x2))))
    y2 = max(0, min(height, round(float(candidate.y2))))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"비어 있는 검출 좌표입니다: [{x1}, {y1}, {x2}, {y2}]")

    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    crop_width = (x2 - x1) * scale
    crop_height = (y2 - y1) * scale
    return (
        max(0, round(center_x - crop_width / 2)),
        max(0, round(center_y - crop_height / 2)),
        min(width, round(center_x + crop_width / 2)),
        min(height, round(center_y + crop_height / 2)),
    )


def make_crops(image: Image.Image, candidate: Candidate) -> tuple[dict[str, Image.Image], dict[str, CropBox]]:
    """x1 · x1.5 · x2 Crop 이미지와 그 영역."""
    crops, boxes = {}, {}
    for name, scale in SCALES:
        box = crop_box(image.width, image.height, candidate, scale)
        crops[name] = image.crop(box)
        boxes[name] = box
    return crops, boxes


def safe_stem(filename: str) -> str:
    """파일 이름에서 확장자를 빼고 영문·숫자·한글·_- 외의 글자를 _ 로 바꾼다. 최대 70자."""
    cleaned = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", Path(filename).stem).strip("_")
    return cleaned[:70] or "image"


def crop_filename(source_name: str, det_index: int, class_id: int, scale: str,
                  confidence: float, group_id: str) -> str:
    """예: beach_123__det01__class1__x1.5__yolo0.8313__ca0323ab.jpg (det 번호는 1부터)"""
    return (
        f"{safe_stem(source_name)}__det{det_index + 1:02d}__class{class_id}"
        f"__{scale}__yolo{confidence:.4f}__{group_id}.jpg"
    )
