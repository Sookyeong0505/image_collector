"""이미지 1장 → 판정 (명세 4-2). DB 와 파일 저장은 모르는 순수 계산이다.

reference 에서 브라우저(App.tsx collect)와 Flask(app.py collect)로 나뉘어 있던 흐름을 한 함수로 이었다.

    이미지 → YOLO 3구역 → 최고 구역 → 피부색 필터 → 3배율 Crop → SwinV2·DINOv2 → 4조건 OR
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from ..config import Settings
from .crops import CropBox, make_crops
from .skin import Candidate, filter_by_skin
from .verifiers import HeadScore, Verifier, dinov2_spec, resolve_device, swinv2_spec
from .yolo import YoloDetector

YOLO_MODEL = Path("yolo") / "best_640n_0522.onnx"   # MODEL_DIR 기준
YOLO_SAVE_THRESHOLD = 0.6                            # R12 의 첫 번째 조건

PIPELINE_NAME = "original"   # image_checks.pipeline. 개선 브랜치 결과와 구분한다


@dataclass
class Detection:
    """피부색 필터를 통과한 검출 1건과 2차 검증 결과."""
    index: int                          # 이미지 안의 순번 (0부터)
    candidate: Candidate
    crop_boxes: dict[str, CropBox]
    crops: dict[str, Image.Image] = field(repr=False)
    swinv2: HeadScore                   # x1 Crop
    dinov2_x15: HeadScore               # x1.5 Crop
    dinov2_x2: HeadScore                # x2 Crop

    @property
    def reasons(self) -> list[str]:
        """R12. 참인 조건의 이름 (reference 의 saveReasons 와 같은 이름)."""
        checks = [
            ("yolo_confidence", self.candidate.confidence >= YOLO_SAVE_THRESHOLD),
            ("swinv2_threshold", self.swinv2.passed),
            ("dinov2_threshold_x1.5", self.dinov2_x15.passed),
            ("dinov2_threshold_x2", self.dinov2_x2.passed),
        ]
        return [name for name, ok in checks if ok]

    @property
    def positive(self) -> bool:
        return bool(self.reasons)


@dataclass
class ImageResult:
    section: str | None        # 채택한 구역. YOLO 박스가 하나도 없으면 None
    yolo_boxes: int            # 채택 구역의 박스 수 (NMS 후)
    detections: list[Detection]
    elapsed_ms: int

    @property
    def verdict(self) -> str:
        return "positive" if any(d.positive for d in self.detections) else "negative"


def load_views(path: Path) -> tuple[Image.Image, Image.Image]:
    """같은 이미지를 두 가지로 읽는다. 둘 다 EXIF 방향을 적용한 RGB 이다.

    - browser: YOLO·피부색용. 브라우저 Canvas 는 완전히 투명한 픽셀을 검정(0,0,0)으로 읽었다.
    - server : Crop·2차 검증용. reference 서버(Pillow convert("RGB"))는 투명도를 그냥 버렸다.
    투명도가 없는 이미지는 두 결과가 같다.
    """
    with Image.open(path) as source:
        upright = ImageOps.exif_transpose(source)
    server = upright.convert("RGB")
    if "A" not in upright.getbands() and "transparency" not in upright.info:
        return server, server

    rgba = np.asarray(upright.convert("RGBA")).copy()
    rgba[rgba[..., 3] == 0, :3] = 0
    return Image.fromarray(rgba[..., :3], "RGB"), server


class Pipeline:
    def __init__(self, yolo: YoloDetector, swinv2: Verifier, dinov2: Verifier):
        self.yolo = yolo
        self.swinv2 = swinv2
        self.dinov2 = dinov2

    @classmethod
    def load(cls, settings: Settings) -> "Pipeline":
        """YOLO 와 head 파일을 읽는다. 백본은 첫 검출 때 올린다. 모델 파일이 없으면 FileNotFoundError."""
        device = resolve_device(settings.detect_device)
        return cls(
            YoloDetector.load(settings.model_dir / YOLO_MODEL, device.type),   # 백본과 같은 장치
            Verifier(swinv2_spec(settings.model_dir), device),
            Verifier(dinov2_spec(settings.model_dir), device),
        )

    def run(self, path: Path) -> ImageResult:
        started = time.perf_counter()
        browser_view, server_view = load_views(path)

        best = self.yolo.find_best(browser_view)
        detections = []
        for index, candidate in enumerate(filter_by_skin(browser_view, best)):
            crops, boxes = make_crops(server_view, candidate)
            [swinv2] = self.swinv2.verify([crops["x1"]], [candidate.class_id])
            dinov2_x15, dinov2_x2 = self.dinov2.verify(
                [crops["x1.5"], crops["x2"]], [candidate.class_id, candidate.class_id]
            )
            detections.append(Detection(index, candidate, boxes, crops, swinv2, dinov2_x15, dinov2_x2))

        return ImageResult(
            section=best.section.name if best.boxes else None,
            yolo_boxes=len(best.boxes),
            detections=detections,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
