"""검출 결과 파일 저장 (명세 5-5). 팀원에게 보내기 위해 검출한 날짜별 폴더에 모은다.

    {DETECT_SAVE_PATH}/20261015_dataset/
    ├── detect_img/      원본 사본 (양성 이미지 1장당 1개)
    ├── class1/ … class5/ 양성 검출 1건당 x1 · x1.5 · x2 Crop 3장 (같은 그룹 ID)
    └── metadata.jsonl   양성 검출 1건 = 1줄

양성 검출만 저장한다. 클래스 폴더는 그 클래스의 양성 검출이 생길 때 만든다.
"""

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .crops import crop_filename, safe_stem
from .pipeline import YOLO_SAVE_THRESHOLD, Detection, ImageResult

ORIGINAL_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}   # 그대로 복사할 확장자


def dataset_dir_name(moment: datetime) -> str:
    """검출을 시작한 현지 시각 → '20261015_dataset'."""
    return f"{moment:%Y%m%d}_dataset"


@dataclass
class SavedFiles:
    dataset_dir: str                                   # 날짜 폴더 이름
    original_copy: str                                 # 날짜 폴더 기준 경로
    crop_groups: dict[int, str] = field(default_factory=dict)   # 검출 순번 → 그룹 ID


def _head(score) -> dict:
    return {"adultScore": round(score.score, 6), "threshold": round(score.threshold, 6), "prediction": score.passed}


def _metadata(source: Path, keyword: str, detection: Detection, original: str,
              saved: dict[str, str], moment: datetime) -> dict:
    """reference 의 metadata.jsonl 과 같은 이름의 필드. 경로는 날짜 폴더 기준."""
    candidate = detection.candidate
    votes = {
        "swinv2_x1": detection.swinv2.passed,
        "dinov2_x1.5": detection.dinov2_x15.passed,
        "dinov2_x2": detection.dinov2_x2.passed,
    }
    return {
        "sourceFile": source.name,
        "keyword": keyword,
        "detectionId": detection.index,
        "classId": candidate.class_id,
        "classLabel": candidate.label,
        "yoloConfidence": candidate.confidence,
        "yoloSaveThreshold": YOLO_SAVE_THRESHOLD,
        "detectionBox": [candidate.x1, candidate.y1, candidate.x2, candidate.y2],
        "cropBoxes": {scale: list(box) for scale, box in detection.crop_boxes.items()},
        "detectedImageFile": original,
        "positiveVotes": sum(votes.values()),
        "modelVotes": votes,
        "saveReasons": detection.reasons,
        "savedFiles": saved,
        "swinv2": _head(detection.swinv2),
        "dinov2": _head(detection.dinov2_x15),
        "dinov2X2": _head(detection.dinov2_x2),
        "pipeline": "original",
        "detectedAt": moment.isoformat(timespec="seconds"),
    }


class DatasetWriter:
    def __init__(self, root: Path):
        self.root = root

    def save(self, source: Path, keyword: str, result: ImageResult, moment: datetime) -> SavedFiles | None:
        """양성 이미지면 원본 사본·Crop·metadata 를 저장하고 경로를 돌려준다. 음성이면 아무것도 하지 않는다.

        moment: 이 이미지 처리를 시작한 현지 시각. 한 이미지의 파일은 모두 이 날짜 폴더에 들어간다.
        """
        positives = [d for d in result.detections if d.positive]
        if not positives:
            return None

        folder = self.root / dataset_dir_name(moment)
        (folder / "detect_img").mkdir(parents=True, exist_ok=True)

        suffix = source.suffix.lower()
        stem = f"{safe_stem(source.name)}__detected__{uuid.uuid4().hex[:8]}"
        if suffix in ORIGINAL_SUFFIXES:
            original = Path("detect_img") / f"{stem}{suffix}"
            shutil.copyfile(source, folder / original)
        else:                                   # 알 수 없는 확장자는 JPEG 로 다시 저장 (reference 와 같다)
            original = Path("detect_img") / f"{stem}.jpg"
            with Image.open(source) as image:
                ImageOps.exif_transpose(image).convert("RGB").save(folder / original, "JPEG", quality=95)

        saved = SavedFiles(folder.name, original.as_posix())
        lines = []
        for detection in positives:
            class_dir = folder / f"class{detection.candidate.class_id}"
            class_dir.mkdir(exist_ok=True)
            group_id = uuid.uuid4().hex[:8]
            files = {}
            for scale, crop in detection.crops.items():
                name = crop_filename(source.name, detection.index, detection.candidate.class_id, scale,
                                     detection.candidate.confidence, group_id)
                crop.save(class_dir / name, "JPEG", quality=95, optimize=True)
                files[scale] = f"{class_dir.name}/{name}"
            saved.crop_groups[detection.index] = group_id
            lines.append(json.dumps(_metadata(source, keyword, detection, saved.original_copy, files, moment),
                                    ensure_ascii=False))

        with open(folder / "metadata.jsonl", "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
        return saved
