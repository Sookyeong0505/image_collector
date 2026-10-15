"""이미지 1장 → 판정: 단계 연결, 4조건 OR 판정, 이미지 읽기(EXIF·투명도)가 reference 와 같은지."""

import pytest

np = pytest.importorskip("numpy")   # 검출 의존성(.[detect])이 없으면 건너뛴다
pytest.importorskip("torch")
from PIL import Image

from app.detector.pipeline import Detection, Pipeline, load_views
from app.detector.skin import Candidate
from app.detector.verifiers import HeadScore
from app.detector.yolo import Box, SectionResult, sections_of

SKIN = (224, 172, 105)
PASS = HeadScore(0.9, 0.5)
FAIL = HeadScore(0.1, 0.5)


def detection(confidence=0.5, swinv2=FAIL, dinov2_x15=FAIL, dinov2_x2=FAIL) -> Detection:
    candidate = Candidate(1, "EXPOSED_BREAST_F", confidence, "full", 0, 0, 10, 10)
    return Detection(0, candidate, {}, {}, swinv2, dinov2_x15, dinov2_x2)


def test_네_조건_중_하나라도_참이면_양성():
    assert not detection().positive
    assert detection(confidence=0.6).reasons == ["yolo_confidence"]          # YOLO 0.6 이상
    assert not detection(confidence=0.5999).positive
    assert detection(swinv2=PASS).reasons == ["swinv2_threshold"]
    assert detection(dinov2_x15=PASS, dinov2_x2=PASS).reasons == ["dinov2_threshold_x1.5", "dinov2_threshold_x2"]


class FakeYolo:
    def __init__(self, boxes):
        self.boxes = boxes

    def find_best(self, image):
        return SectionResult(sections_of(image.width, image.height)[0], self.boxes)


class FakeVerifier:
    """받은 Crop 크기를 기록하고 정해 둔 점수를 돌려준다."""

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def verify(self, images, class_ids):
        self.calls.append(([image.size for image in images], class_ids))
        return self.scores[:len(images)]


def save_skin_image(path, size=(200, 100)):
    Image.new("RGB", size, SKIN).save(path)
    return path


def test_피부색_통과한_검출마다_세_배율을_두_모델에_넣는다(tmp_path):
    image = save_skin_image(tmp_path / "a.png")
    swinv2, dinov2 = FakeVerifier([FAIL]), FakeVerifier([FAIL, PASS])
    pipeline = Pipeline(FakeYolo([Box(3, 0.45, 40, 40, 60, 60)]), swinv2, dinov2)

    result = pipeline.run(image)

    assert swinv2.calls == [([(20, 20)], [3])]                      # x1
    assert dinov2.calls == [([(30, 30), (40, 40)], [3, 3])]          # x1.5, x2
    assert result.section == "full" and result.yolo_boxes == 1
    assert result.detections[0].reasons == ["dinov2_threshold_x2"]
    assert result.verdict == "positive"


def test_박스가_없거나_피부색이_없으면_음성(tmp_path):
    blue = tmp_path / "blue.png"
    Image.new("RGB", (200, 100), (0, 0, 255)).save(blue)
    nothing = Pipeline(FakeYolo([]), FakeVerifier([]), FakeVerifier([]))
    result = nothing.run(blue)
    assert (result.verdict, result.section, result.detections) == ("negative", None, [])

    no_skin = Pipeline(FakeYolo([Box(1, 0.9, 10, 10, 50, 50)]), FakeVerifier([PASS]), FakeVerifier([PASS, PASS]))
    result = no_skin.run(blue)
    assert result.verdict == "negative" and result.yolo_boxes == 1 and result.detections == []


def test_EXIF_방향을_적용해서_읽는다(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6                                  # 시계 방향 90도 회전해서 보라는 표시
    Image.new("RGB", (200, 100)).save(path, exif=exif)
    browser, server = load_views(path)
    assert browser.size == server.size == (100, 200)


def test_완전히_투명한_픽셀은_YOLO용에서만_검정이다(tmp_path):
    path = tmp_path / "alpha.png"
    rgba = Image.new("RGBA", (2, 1), (200, 100, 50, 255))
    rgba.putpixel((1, 0), (200, 100, 50, 0))          # 색은 남아 있지만 완전히 투명
    rgba.save(path)
    browser, server = load_views(path)
    assert browser.getpixel((1, 0)) == (0, 0, 0)      # Canvas 처럼
    assert server.getpixel((1, 0)) == (200, 100, 50)  # Pillow convert("RGB") 처럼
    assert browser.getpixel((0, 0)) == server.getpixel((0, 0)) == (200, 100, 50)
