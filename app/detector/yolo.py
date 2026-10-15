"""YOLO 1차 검출 (명세 R2~R7).

reference/src/App.tsx 의 preprocessSection, runDetection, calculateIoU, applyNms, findBestCandidate 를 옮겼다.
브라우저에서 돌던 것을 onnxruntime(CPU)으로 실행한다는 점만 다르다.

    이미지 → full · left · right 구역 → 구역마다 640×640 으로 늘려 추론 → 점수·클래스 필터 → NMS
          → 구역별 최고 점수를 비교해 한 구역만 채택
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

CLASSES = (
    "EXPOSED_BELLY",
    "EXPOSED_BREAST_F",
    "EXPOSED_BREAST_M",
    "EXPOSED_BUTTOCKS",
    "EXPOSED_GENITALIA_F",
    "EXPOSED_GENITALIA_M",
)
TARGET_CLASS_IDS = (1, 3, 4, 5)
INPUT_SIZE = 640
CONFIDENCE_THRESHOLD = 0.3
IOU_THRESHOLD = 0.7


@dataclass(frozen=True)
class Section:
    """이미지에서 YOLO 에 따로 넣는 영역. 오른쪽 절반은 폭이 홀수면 x 가 0.5 로 끝난다."""
    name: str   # 'full' | 'left' | 'right'
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Box:
    """YOLO 박스 1개. 좌표는 구역 안의 좌표(구역 왼쪽 위가 0,0)."""
    class_id: int
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def label(self) -> str:
        return CLASSES[self.class_id]


@dataclass(frozen=True)
class SectionResult:
    section: Section
    boxes: list[Box]

    @property
    def score(self) -> float:
        """구역의 대표 점수 = 박스 중 최고 confidence. 박스가 없으면 0."""
        return max([0.0, *(box.confidence for box in self.boxes)])


def sections_of(width: int, height: int) -> list[Section]:
    """R2. 전체, 왼쪽 절반, 오른쪽 절반. 이 순서가 동점일 때의 우선순위다."""
    return [
        Section("full", 0, 0, width, height),
        Section("left", 0, 0, width / 2, height),
        Section("right", width / 2, 0, width / 2, height),
    ]


def preprocess(image: Image.Image, section: Section) -> np.ndarray:
    """R3. 구역을 비율을 무시하고 640×640 으로 늘린 뒤 /255 → [1, 3, 640, 640] float32 (RGB).

    브라우저는 Canvas drawImage 로 늘렸다. 보간 방식이 달라 점수가 미세하게 다를 수 있다 (명세 9-2 에서 비교).
    """
    box = (section.x, section.y, section.x + section.width, section.y + section.height)
    resized = image.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR, box=box)
    pixels = np.asarray(resized, dtype=np.float32) / 255
    return pixels.transpose(2, 0, 1)[np.newaxis]   # HWC → NCHW


def parse_output(output: np.ndarray, section: Section) -> list[Box]:
    """R4·R5. YOLOv8 출력 [1, 4+클래스 수, 박스 수] 를 구역 좌표의 박스 목록으로 바꾼다.

    박스마다 클래스 점수가 가장 높은 클래스를 그 박스의 클래스로 본다 (동점이면 앞 번호).
    """
    columns = 4 + len(CLASSES)
    data = np.asarray(output, dtype=np.float64).reshape(-1)   # JS 는 float32 를 double 로 비교했다
    if data.size % columns:
        raise ValueError(f"YOLO 출력 크기({data.size})가 예상 형식(열 {columns}개)과 다릅니다")
    grid = data.reshape(columns, -1)

    class_ids = grid[4:].argmax(axis=0)
    confidences = grid[4:].max(axis=0)
    scale_x = section.width / INPUT_SIZE
    scale_y = section.height / INPUT_SIZE

    boxes = []
    for index in np.flatnonzero(confidences >= CONFIDENCE_THRESHOLD):
        class_id = int(class_ids[index])
        if class_id not in TARGET_CLASS_IDS:
            continue
        cx, cy, w, h = grid[:4, index]
        boxes.append(Box(
            class_id=class_id,
            confidence=float(confidences[index]),
            x1=(cx - w / 2) * scale_x,
            y1=(cy - h / 2) * scale_y,
            x2=(cx + w / 2) * scale_x,
            y2=(cy + h / 2) * scale_y,
        ))
    return boxes


def iou(first: Box, second: Box) -> float:
    x1 = max(first.x1, second.x1)
    y1 = max(first.y1, second.y1)
    x2 = min(first.x2, second.x2)
    y2 = min(first.y2, second.y2)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    first_area = (first.x2 - first.x1) * (first.y2 - first.y1)
    second_area = (second.x2 - second.x1) * (second.y2 - second.y1)
    return intersection / (first_area + second_area - intersection)


def apply_nms(boxes: list[Box]) -> list[Box]:
    """R6. 점수 높은 박스부터 고르고, 같은 클래스이면서 IoU ≥ 0.7 인 나머지를 지운다.

    정렬은 안정 정렬이라 점수가 같으면 원래 순서를 지킨다 (JS Array.sort 와 같다).
    """
    remaining = sorted(boxes, key=lambda box: box.confidence, reverse=True)
    selected = []
    while remaining:
        best = remaining.pop(0)
        selected.append(best)
        remaining = [
            box for box in remaining
            if not (box.class_id == best.class_id and iou(best, box) >= IOU_THRESHOLD)
        ]
    return selected


def pick_best(results: list[SectionResult]) -> SectionResult:
    """R7. 대표 점수가 가장 높은 구역. 동점이면 앞의 구역(full → left → right)."""
    best = results[0]
    for result in results[1:]:
        if result.score > best.score:
            best = result
    return best


class YoloDetector:
    def __init__(self, session):
        """session: onnxruntime.InferenceSession (테스트에서는 run() 만 있는 가짜)."""
        self.session = session
        self.input_name = session.get_inputs()[0].name

    @classmethod
    def load(cls, model_path: Path) -> "YoloDetector":
        import onnxruntime as ort

        if not model_path.is_file():
            raise FileNotFoundError(f"YOLO 모델 파일이 없습니다: {model_path}")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        return cls(session)

    def detect_section(self, image: Image.Image, section: Section) -> SectionResult:
        output = self.session.run(None, {self.input_name: preprocess(image, section)})[0]
        return SectionResult(section, apply_nms(parse_output(output, section)))

    def find_best(self, image: Image.Image) -> SectionResult:
        """세 구역을 모두 추론하고 한 구역만 고른다. image 는 RGB."""
        return pick_best([self.detect_section(image, s) for s in sections_of(image.width, image.height)])
