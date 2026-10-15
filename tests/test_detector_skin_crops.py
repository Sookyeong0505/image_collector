"""피부색 필터, 좌표 변환, 3배율 Crop, 파일 이름이 reference 와 같은 규칙인지."""

import pytest

np = pytest.importorskip("numpy")   # 검출 의존성(.[detect])이 없으면 건너뛴다
from PIL import Image

from app.detector.crops import crop_box, crop_filename, make_crops, safe_stem
from app.detector.skin import Candidate, filter_by_skin, is_skin, js_round, skin_ratio
from app.detector.yolo import Box, SectionResult, sections_of

SKIN = (224, 172, 105)   # Cr 159.5, Cb 85.7 → 피부색 범위 안
BLUE = (0, 0, 255)


def pixels(colors: list[tuple[int, int, int]]) -> np.ndarray:
    return np.array([colors], dtype=np.uint8)   # 높이 1 × 너비 N


def candidate(x1=10, y1=10, x2=20, y2=20, confidence=0.5) -> Candidate:
    return Candidate(1, "EXPOSED_BREAST_F", confidence, "full", x1, y1, x2, y2)


def test_피부색_비율():
    assert skin_ratio(pixels([SKIN, SKIN, BLUE, BLUE])) == 0.5
    assert skin_ratio(np.zeros((0, 0, 3), dtype=np.uint8)) == 0


def test_피부색은_15퍼센트를_넘어야_통과():
    exactly_15 = pixels([SKIN] * 3 + [BLUE] * 17)
    assert not is_skin(exactly_15)
    over_15 = pixels([SKIN] * 4 + [BLUE] * 16)
    assert is_skin(over_15)


def test_JS_반올림은_0점5를_항상_올린다():
    assert [js_round(v) for v in (2.5, 3.5, -0.5, 2.4999)] == [3, 4, 0, 2]


def test_피부색이_있는_박스만_원본_좌표로_바꾼다():
    image = Image.new("RGB", (101, 40), BLUE)
    image.paste(SKIN, (60, 0, 101, 40))             # 오른쪽만 피부색
    _, _, right = sections_of(101, 40)               # 오른쪽 구역은 x = 50.5 에서 시작
    result = SectionResult(right, [
        Box(1, 0.59996, 10.2, 5.7, 20.1, 15.0),      # 구역 x 10~21 → 원본 60.5~71.5 (피부)
        Box(3, 0.9, -5, 0, 5, 10),                   # 구역 x 0~5 → 원본 50~55 (파랑)
    ])
    [found] = filter_by_skin(image, result)
    assert found.section == "right"
    assert found.confidence == 0.6                   # 브라우저처럼 넷째 자리 반올림
    # floor/ceil 한 구역 좌표 + 50.5 를 JS 방식으로 반올림
    assert (found.x1, found.y1, found.x2, found.y2) == (js_round(60.5), 5, js_round(71.5), 15)


def test_구역_밖으로_나간_박스는_구역_경계에서_자르고_비면_버린다():
    image = Image.new("RGB", (100, 50), SKIN)
    full, _, _ = sections_of(100, 50)
    result = SectionResult(full, [
        Box(1, 0.9, -10, -10, 30, 30),               # 0~30 으로 잘림
        Box(1, 0.9, 120, 10, 140, 20),               # 완전히 밖 → 버림
    ])
    [found] = filter_by_skin(image, result)
    assert (found.x1, found.y1, found.x2, found.y2) == (0, 0, 30, 30)


def test_Crop은_중심을_고정하고_배율만큼_넓힌다():
    assert crop_box(200, 200, candidate(), 1.0) == (10, 10, 20, 20)
    assert crop_box(200, 200, candidate(), 2.0) == (5, 5, 25, 25)
    assert crop_box(200, 200, candidate(), 1.5) == (8, 8, 22, 22)   # 7.5→8, 22.5→22 (파이썬 반올림)


def test_Crop은_이미지_경계에서_자른다():
    assert crop_box(30, 30, candidate(x1=0, y1=0, x2=20, y2=20), 2.0) == (0, 0, 30, 30)


def test_빈_검출_좌표는_에러():
    with pytest.raises(ValueError):
        crop_box(100, 100, candidate(x1=20, x2=20), 1.0)


def test_세_배율을_한_번에_자른다():
    crops, boxes = make_crops(Image.new("RGB", (200, 200)), candidate())
    assert list(crops) == ["x1", "x1.5", "x2"]
    assert crops["x2"].size == (20, 20) and boxes["x2"] == (5, 5, 25, 25)


def test_파일_이름_규칙():
    assert safe_stem("비키니 사진(1).JPG") == "비키니_사진_1"
    assert safe_stem("!!!.jpg") == "image"
    assert crop_filename("beach_123.jpg", 0, 1, "x1.5", 0.83125, "ca0323ab") == (
        "beach_123__det01__class1__x1.5__yolo0.8313__ca0323ab.jpg"   # det 번호는 1부터
    )
