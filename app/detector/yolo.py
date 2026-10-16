"""YOLO 1차 검출 (명세 R2~R7).

reference/src/App.tsx 의 preprocessSection, runDetection, calculateIoU, applyNms, findBestCandidate 를 옮겼다.
브라우저에서 돌던 것을 onnxruntime(CPU)으로 실행한다는 점만 다르다.

    이미지 → full · left · right 구역 → 구역마다 640×640 으로 늘려 추론 → 점수·클래스 필터 → NMS
          → 구역별 최고 점수를 비교해 한 구역만 채택
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

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


def mip_level(section: Section) -> int:
    """구역을 640 으로 줄일 때 쓸 밉맵 단계. 가로·세로 중 덜 줄어드는 쪽 배율로 정한다.

    예: 6720×4480 → 640×640 이면 배율은 10.5, 7 → 작은 쪽 7 → 2단계(1/4 크기)부터 샘플링한다.
    """
    scale = min(section.width, section.height) / INPUT_SIZE
    return max(0, math.floor(math.log2(scale))) if scale >= 1 else 0


def _bilinear(pixels: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """픽셀 중심 기준 좌표(xs, ys)에서 bilinear 로 값을 읽는다. 이미지 밖은 가장자리 값."""
    height, width = pixels.shape[:2]
    u = np.clip(xs - 0.5, 0, width - 1)
    v = np.clip(ys - 0.5, 0, height - 1)
    x0, y0 = np.floor(u).astype(np.intp), np.floor(v).astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    wx, wy = (u - x0)[np.newaxis, :, np.newaxis], (v - y0)[:, np.newaxis, np.newaxis]
    # 원본 행 하나의 가로 보간 결과는 어느 출력 행에서 쓰든 같다. 쓰이는 원본 행만 한 번씩 계산해 재사용한다.
    # 계산 순서(가로 → 세로, float64)는 그대로라 결과는 한 비트도 바뀌지 않는다.
    rows = np.unique(np.concatenate([y0, y1]))
    source = pixels[rows]
    horizontal = np.multiply(source[:, x0], 1 - wx, dtype=np.float64)
    horizontal += source[:, x1] * wx
    result = horizontal[np.searchsorted(rows, y0)] * (1 - wy)
    result += horizontal[np.searchsorted(rows, y1)] * wy
    np.round(result, out=result)
    return np.clip(result, 0, 255, out=result).astype(np.uint8)


def mip_of(image: Image.Image, level: int, cache: dict | None = None) -> Image.Image:
    """이미지를 level 번 절반으로 줄인 사본(2×2 평균). cache 를 주면 같은 이미지의 세 구역이 함께 쓴다."""
    if cache is None:
        cache = {}
    cache.setdefault(0, image)
    done = max(step for step in cache if step <= level)
    mip = cache[done]
    for step in range(done + 1, level + 1):
        if mip.width < 2 or mip.height < 2:
            break
        mip = cache[step] = mip.reduce(2)
    return mip


def resize_like_canvas(image: Image.Image, section: Section, cache: dict | None = None) -> np.ndarray:
    """구역을 640×640 으로 늘린 RGB uint8 배열. 브라우저(Chrome) Canvas drawImage 와 가깝게 만든다.

    Chrome 은 크게 줄일 때 이미지 전체를 절반씩 줄인 사본(밉맵)을 만들고, 알맞은 단계에서 bilinear 로 읽는다.
    Pillow 의 BILINEAR(안티앨리어싱)로 줄이면 YOLO 점수가 평균 0.04 달라졌고, 이 방식은 0.007 이었다 (명세 9-2).
    """
    mip = mip_of(image, mip_level(section), cache)
    fx, fy = image.width / mip.width, image.height / mip.height
    steps = (np.arange(INPUT_SIZE) + 0.5) / INPUT_SIZE
    xs = (section.x + steps * section.width) / fx
    ys = (section.y + steps * section.height) / fy
    return _bilinear(np.asarray(mip), xs, ys)


def preprocess(image: Image.Image, section: Section, cache: dict | None = None) -> np.ndarray:
    """R3. 구역을 비율을 무시하고 640×640 으로 늘린 뒤 /255 → [1, 3, 640, 640] float32 (RGB)."""
    pixels = resize_like_canvas(image, section, cache).astype(np.float32) / 255
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
        # 세 구역의 리사이즈를 동시에 한다. NumPy 계산은 GIL 을 풀어 주므로 스레드로도 여러 코어를 쓴다.
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="yolo-resize")

    @classmethod
    def load(cls, model_path: Path, device: str = "cpu") -> "YoloDetector":
        """device 가 'cuda' 면 GPU 로 추론한다 (onnxruntime-gpu 필요). 쓸 수 없으면 CPU 로 돌고 로그를 남긴다.

        GPU 추론은 CPU 와 소수점 끝자리가 조금 다르다 (점수 차이 약 0.000001). reference 비교는 명세 9-2.
        """
        if device == "cuda":
            # onnxruntime-gpu 는 torch 가 함께 깔아 둔 CUDA 12 · cuDNN 9 DLL 을 쓴다. torch 를 먼저 불러와야 찾는다.
            import torch  # noqa: F401
        import onnxruntime as ort

        if not model_path.is_file():
            raise FileNotFoundError(f"YOLO 모델 파일이 없습니다: {model_path}")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        if device == "cuda":
            # DEFAULT: 매번 같은 합성곱 알고리즘을 써서 실행할 때마다 결과가 흔들리지 않게 한다
            cuda = ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT", "use_tf32": 0})
            providers = [cuda, "CPUExecutionProvider"]
        session = ort.InferenceSession(str(model_path), options, providers=providers)
        if device == "cuda" and session.get_providers()[0] != "CUDAExecutionProvider":
            log.warning("YOLO 를 GPU 로 돌릴 수 없어 CPU 로 돌립니다 (onnxruntime-gpu 와 CUDA 12 · cuDNN 9 확인)")
        else:
            log.info("YOLO 실행 장치: %s", session.get_providers()[0])
        return cls(session)

    def detect_section(self, image: Image.Image, section: Section, cache: dict | None = None) -> SectionResult:
        output = self.session.run(None, {self.input_name: preprocess(image, section, cache)})[0]
        return SectionResult(section, apply_nms(parse_output(output, section)))

    def find_best(self, image: Image.Image) -> SectionResult:
        """세 구역을 모두 추론하고 한 구역만 고른다. image 는 RGB.

        밉맵은 세 구역이 함께 쓰도록 먼저 만들고, 리사이즈는 세 구역을 동시에 한다.
        YOLO 는 구역 순서대로 하나씩 돌린다 (첫 구역 추론 중에 나머지 구역 리사이즈가 이어진다).
        """
        sections = sections_of(image.width, image.height)
        cache: dict = {}
        for section in sections:
            mip_of(image, mip_level(section), cache)   # 스레드들이 같은 사본을 읽기만 하도록 미리 만든다
        tensors = [self._pool.submit(preprocess, image, section, cache) for section in sections]
        results = []
        for section, tensor in zip(sections, tensors):
            output = self.session.run(None, {self.input_name: tensor.result()})[0]
            results.append(SectionResult(section, apply_nms(parse_output(output, section))))
        return pick_best(results)
