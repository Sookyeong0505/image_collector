"""이미지 검사·해시·저장.

  - inspect_image : 받은 바이트가 진짜 이미지인지 확인하고 해시 2종을 계산한다
  - hamming       : 지각 해시 두 개가 몇 비트 다른지
  - save_image    : 임시 파일에 쓴 뒤 이름을 바꿔 저장한다
"""

import hashlib
import io
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .http import SkipError

# 확장자와 content-type 은 자주 틀리므로 Pillow 가 실제 바이트로 판별한 형식을 쓴다
_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp", "MPO": ".jpg"}


@dataclass(frozen=True)
class ImageInfo:
    extension: str   # '.jpg' 처럼 점 포함
    width: int
    height: int
    size: int        # 바이트 수
    sha256: str      # L1: 완전히 같은 파일인지
    phash: str       # L2: 리사이즈·재압축본인지 (16진수 16자)


def inspect_image(data: bytes, min_bytes: int = 2048) -> ImageInfo:
    """이미지가 아니면 SkipError. CPU 를 쓰는 작업이라 호출하는 쪽에서 스레드로 돌린다."""
    if len(data) < min_bytes:
        raise SkipError(f"용량 미달 ({len(data)}B) — 에러 페이지 가능성")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)  # 비정상적으로 큰 이미지는 거부
            with Image.open(io.BytesIO(data)) as image:
                extension = _EXTENSIONS.get(image.format or "")
                if extension is None:
                    raise SkipError(f"지원하지 않는 형식 ({image.format})")
                width, height = image.size
                phash = _dhash(image)
    except SkipError:
        raise
    except Exception as err:  # Pillow 는 깨진 파일에 여러 종류의 예외를 낸다
        raise SkipError(f"이미지가 아니거나 깨짐 ({type(err).__name__})") from err

    return ImageInfo(extension, width, height, len(data), hashlib.sha256(data).hexdigest(), phash)


def _dhash(image: Image.Image) -> str:
    """차이 해시(dHash): 이미지를 9×8 흑백으로 줄이고, 옆 칸보다 밝은지를 64비트로 적는다.

    크기를 바꾸거나 다시 압축해도 '어디가 더 밝은가'는 거의 그대로라서
    같은 사진이면 해시가 몇 비트만 다르다.
    """
    # JPEG 는 디코딩 단계에서 미리 축소할 수 있다. 큰 원본을 통째로 펼치지 않아 RAM 을 아낀다.
    image.draft("L", (64, 64))
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = small.tobytes()

    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:016x}"


def hamming(a: int, b: int) -> int:
    """두 해시에서 서로 다른 비트 수. 0 이면 동일, 작을수록 비슷한 이미지."""
    return (a ^ b).bit_count()


def safe_name(text: str) -> str:
    """폴더·파일 이름에 쓸 수 없는 문자와 공백을 _ 로 바꾼다."""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text.strip())


def relative_path(keyword: str, site: str, source_id: str, extension: str) -> str:
    """저장 폴더 기준 상대 경로: {키워드}/{사이트}/{키워드}_{출처ID}.{확장자}"""
    name = safe_name(keyword)
    return f"{name}/{site}/{name}_{safe_name(source_id)}{extension}"


def save_image(base: Path, relative: str, data: bytes) -> None:
    """임시 파일(.part)에 다 쓴 뒤 이름을 바꾼다. 쓰는 도중 죽어도 반쪽짜리 이미지가 남지 않는다."""
    final = base / relative
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(final.name + ".part")
    temp.write_bytes(data)
    os.replace(temp, final)


def remove_partial_files(base: Path) -> int:
    """쓰다 만 임시 파일(.part)을 지우고 지운 개수를 돌려준다.

    이미지를 쓰는 도중에 프로세스가 강제 종료되면 .part 가 남는다.
    수집기가 시작할 때 한 번 호출한다 (잠금을 얻은 뒤라서 다른 수집기가 쓰는 중인 파일일 수 없다).
    """
    if not base.exists():
        return 0
    removed = 0
    for path in base.rglob("*.part"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass  # 지우지 못해도 수집에는 지장이 없다
    return removed
