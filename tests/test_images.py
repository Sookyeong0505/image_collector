import pytest

from app.collector.http import SkipError
from app.collector.images import hamming, inspect_image, relative_path, save_image
from tests.conftest import make_image


def distance(a: bytes, b: bytes) -> int:
    return hamming(int(inspect_image(a).phash, 16), int(inspect_image(b).phash, 16))


def test_이미지_정보를_읽는다():
    data = make_image(1, size=200)
    info = inspect_image(data)

    assert (info.extension, info.width, info.height, info.size) == (".jpg", 200, 200, len(data))
    assert len(info.sha256) == 64 and len(info.phash) == 16


def test_확장자는_실제_형식을_따른다():
    assert inspect_image(make_image(1, fmt="PNG")).extension == ".png"
    assert inspect_image(make_image(1, fmt="WEBP"), min_bytes=100).extension == ".webp"


def test_크기나_화질이_달라도_같은_사진은_지각_해시가_가깝다():
    original = make_image(1, size=512, quality=95)

    assert distance(original, make_image(1, size=200, quality=95)) <= 5    # 리사이즈
    assert distance(original, make_image(1, size=512, quality=40)) <= 5    # 재압축
    assert distance(original, make_image(1, size=300, fmt="PNG")) <= 5     # 형식 변환


def test_다른_사진은_지각_해시가_멀다():
    assert all(distance(make_image(1), make_image(seed)) > 10 for seed in range(2, 12))


def test_파일이_달라지면_SHA는_달라진다():
    assert inspect_image(make_image(1, quality=90)).sha256 != inspect_image(make_image(1, quality=89)).sha256


@pytest.mark.parametrize("data", [
    b"<html>403 Forbidden</html>" * 200,     # 이미지인 척하는 에러 페이지
    make_image(1)[:1500] ,                   # 용량 미달
    b"\xff\xd8\xff" + b"\x00" * 5000,        # JPEG 머리만 있고 내용이 깨짐
])
def test_이미지가_아니면_건너뛴다(data):
    with pytest.raises(SkipError):
        inspect_image(data)


def test_저장_경로_규칙():
    assert relative_path("muscular man", "pexels", "123", ".jpg") == "muscular_man/pexels/muscular_man_123.jpg"
    assert relative_path('a/b:c?', "naver", "x", ".png") == "a_b_c_/naver/a_b_c__x.png"


def test_저장하면_임시_파일이_남지_않는다(tmp_path):
    save_image(tmp_path, "beach/pexels/beach_1.jpg", b"data")

    folder = tmp_path / "beach" / "pexels"
    assert [path.name for path in folder.iterdir()] == ["beach_1.jpg"]
    assert (folder / "beach_1.jpg").read_bytes() == b"data"
