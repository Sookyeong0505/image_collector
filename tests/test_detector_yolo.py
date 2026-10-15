"""YOLO 1차 검출: 출력 해석, NMS, 구역 선택이 reference(App.tsx)와 같은 규칙인지."""

import pytest

np = pytest.importorskip("numpy")   # 검출 의존성(.[detect])이 없으면 건너뛴다
from PIL import Image

from app.config import PROJECT_ROOT
from app.detector.yolo import (
    INPUT_SIZE, Box, Section, SectionResult, YoloDetector, apply_nms, iou, parse_output, pick_best,
    mip_level, preprocess, resize_like_canvas, sections_of,
)

FULL = Section("full", 0, 0, 1280, 640)   # 640 입력 기준 가로 2배, 세로 1배
MODEL = PROJECT_ROOT / "models" / "yolo" / "best_640n_0522.onnx"


def make_output(*rows: tuple[float, float, float, float, list[float]]) -> np.ndarray:
    """(cx, cy, w, h, [클래스 6개 점수]) 들로 YOLOv8 모양 [1, 10, 박스 수] 출력을 만든다."""
    grid = np.zeros((10, len(rows)), dtype=np.float32)
    for index, (cx, cy, w, h, scores) in enumerate(rows):
        grid[:4, index] = (cx, cy, w, h)
        grid[4:, index] = scores
    return grid[np.newaxis]


def box(class_id=1, confidence=0.9, x1=0.0, y1=0.0, x2=10.0, y2=10.0) -> Box:
    return Box(class_id, confidence, x1, y1, x2, y2)


def test_출력을_구역_좌표의_박스로_바꾼다():
    output = make_output((100, 200, 40, 20, [0, 0.8, 0, 0, 0, 0]))
    [found] = parse_output(output, FULL)
    assert found.class_id == 1 and found.label == "EXPOSED_BREAST_F"
    assert found.confidence == pytest.approx(0.8)
    assert (found.x1, found.y1, found.x2, found.y2) == pytest.approx((160, 190, 240, 210))   # x 만 2배


def test_가장_높은_클래스_점수로_클래스를_정한다():
    [found] = parse_output(make_output((10, 10, 4, 4, [0, 0.4, 0, 0.7, 0, 0])), FULL)
    assert found.class_id == 3


def test_점수_0점3_미만과_대상이_아닌_클래스는_버린다():
    output = make_output(
        (10, 10, 4, 4, [0.9, 0, 0, 0, 0, 0]),     # 클래스 0 (대상 아님)
        (10, 10, 4, 4, [0, 0, 0.9, 0, 0, 0]),     # 클래스 2 (대상 아님)
        (10, 10, 4, 4, [0, 0.29, 0, 0, 0, 0]),    # 0.3 미만
        (10, 10, 4, 4, [0, 0, 0, 0, 0.3, 0]),     # 정확히 0.3 은 통과
    )
    assert [found.class_id for found in parse_output(output, FULL)] == [4]


def test_출력_크기가_맞지_않으면_에러():
    with pytest.raises(ValueError):
        parse_output(np.zeros(11, dtype=np.float32), FULL)


def test_IoU():
    assert iou(box(x1=0, x2=10), box(x1=5, x2=15)) == pytest.approx(50 / 150)
    assert iou(box(x1=0, x2=10), box(x1=10, x2=20)) == 0


def test_NMS는_같은_클래스의_겹친_박스만_지운다():
    strong = box(class_id=1, confidence=0.9)
    overlapped = box(class_id=1, confidence=0.8, x1=0.5)        # IoU 0.95
    other_class = box(class_id=3, confidence=0.7)               # 같은 자리지만 클래스가 다름
    apart = box(class_id=1, confidence=0.6, x1=50, x2=60)
    assert apply_nms([apart, overlapped, other_class, strong]) == [strong, other_class, apart]


def test_NMS_기준은_IoU_0점7_이상():
    first = box(confidence=0.9, x1=0, x2=10)
    just_below = box(confidence=0.8, x1=0, x2=6.9)   # IoU 0.69 → 남는다
    assert apply_nms([first, just_below]) == [first, just_below]
    exactly = box(confidence=0.8, x1=0, x2=7)        # IoU 0.7 → 지운다
    assert apply_nms([first, exactly]) == [first]


def test_구역은_전체_왼쪽_오른쪽_순서이고_홀수_폭은_0점5로_나뉜다():
    full, left, right = sections_of(101, 50)
    assert (full.name, left.name, right.name) == ("full", "left", "right")
    assert (left.x, left.width) == (0, 50.5)
    assert (right.x, right.width) == (50.5, 50.5)


def test_최고_점수_구역_하나만_고르고_동점이면_앞_구역():
    full, left, right = sections_of(100, 100)
    results = [
        SectionResult(full, [box(confidence=0.5)]),
        SectionResult(left, [box(confidence=0.7)]),
        SectionResult(right, [box(confidence=0.7)]),
    ]
    assert pick_best(results).section.name == "left"
    empty = [SectionResult(s, []) for s in (full, left, right)]
    assert pick_best(empty).section.name == "full"


def test_전처리는_비율_무시하고_640으로_늘린_RGB_NCHW():
    image = Image.new("RGB", (300, 100), (255, 0, 0))
    tensor = preprocess(image, Section("left", 0, 0, 150, 100))
    assert tensor.shape == (1, 3, INPUT_SIZE, INPUT_SIZE) and tensor.dtype == np.float32
    assert tensor[0, 0].min() == pytest.approx(1) and tensor[0, 1].max() == 0   # 빨강 채널만 1


def test_밉맵_단계는_덜_줄어드는_쪽_배율로_정한다():
    assert mip_level(Section("full", 0, 0, 6720, 4480)) == 2     # 배율 10.5, 7 → 7 → 2단계
    assert mip_level(Section("left", 0, 0, 3360, 4480)) == 2     # 5.25 → 2단계
    assert mip_level(Section("full", 0, 0, 1279, 3000)) == 0     # 1.998 → 0단계
    assert mip_level(Section("full", 0, 0, 300, 200)) == 0       # 확대는 밉맵을 쓰지 않는다


def test_Canvas식_리사이즈는_밉맵_단계에서_픽셀_중심을_읽는다():
    image = Image.new("RGB", (2560, 1280))
    image.paste((255, 255, 255), (0, 0, 1280, 1280))             # 왼쪽 절반 흰색
    pixels = resize_like_canvas(image, Section("full", 0, 0, 2560, 1280))
    assert pixels.shape == (640, 640, 3) and pixels.dtype == np.uint8
    assert pixels[0, 0].tolist() == [255, 255, 255] and pixels[0, -1].tolist() == [0, 0, 0]
    assert pixels[0, 319].tolist() == [255] * 3 and pixels[0, 320].tolist() == [0] * 3   # 경계가 정확히 가운데


class FakeSession:
    """구역마다 정해 둔 출력을 돌려주는 가짜 onnxruntime 세션."""

    class Input:
        name = "images"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def get_inputs(self):
        return [self.Input()]

    def run(self, _names, feeds):
        assert feeds["images"].shape == (1, 3, INPUT_SIZE, INPUT_SIZE)
        self.calls += 1
        return [self.outputs.pop(0)]


def test_세_구역을_추론해_최고_구역을_고른다():
    nothing = make_output((10, 10, 4, 4, [0, 0, 0, 0, 0, 0]))
    found = make_output((320, 320, 64, 64, [0, 0.6, 0, 0, 0, 0]))
    session = FakeSession([nothing, nothing, found])          # full, left, right 순서
    best = YoloDetector(session).find_best(Image.new("RGB", (200, 100)))
    assert session.calls == 3
    assert best.section.name == "right"
    assert (best.boxes[0].x1, best.boxes[0].x2) == pytest.approx((45, 55))   # 구역 폭 100 / 640


@pytest.mark.skipif(not MODEL.is_file(), reason="models/yolo 에 ONNX 모델이 없다")
def test_실제_모델로_추론된다():
    pytest.importorskip("onnxruntime")
    detector = YoloDetector.load(MODEL)
    result = detector.find_best(Image.new("RGB", (800, 600), (128, 128, 128)))
    assert result.section.name in ("full", "left", "right")
    assert all(found.class_id in (1, 3, 4, 5) for found in result.boxes)
