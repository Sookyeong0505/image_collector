"""검출 워커 1회 실행 (명세 5-3). python -m app detect 가 부른다.

    잠금 → 끊긴 작업 복구 → [켜짐 + 동작 시간 안?] → 할 일 있는 작업 → 이미지 1장 처리 → 반복
                                     └ 아니면 종료          └ 없으면 종료

수집기(serve)가 조건이 맞을 때 이 프로세스를 띄우고, 조건이 끝나면 이 프로세스가 스스로 끝난다.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PIL import UnidentifiedImageError

from ..db import repo
from ..timeutil import to_db, utcnow
from .dataset import DatasetWriter
from .pipeline import PIPELINE_NAME, ImageResult, Pipeline
from .supervisor import detection_allowed

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 5    # 예상 못한 오류가 이만큼 이어지면 종료 (수집기가 10분 뒤 다시 띄운다)
MAX_CONSECUTIVE_MISSING = 20  # 이미지 파일이 이만큼 연속으로 없으면 설정 문제로 보고 종료
IMAGE_ERRORS = (UnidentifiedImageError, OSError)   # 이미지 한 장의 문제 (깨진 파일 등)


class TooManyErrors(Exception):
    pass


class Worker:
    def __init__(self, conn, load_pipeline: Callable[[], Pipeline], writer: DatasetWriter | None,
                 image_root: Path, *,
                 ignore_schedule: bool = False,
                 now_local: Callable[[], datetime] = datetime.now,
                 now_utc: Callable[[], datetime] = utcnow):
        """load_pipeline: 처음 검출할 때 한 번 부른다. 남은 작업을 정리만 할 때는 모델을 올리지 않는다.
        writer: None 이면 결과 파일을 저장하지 않는다 (DETECT_SAVE_CROPS=false).
        image_root: 수집 이미지 폴더 (.env 의 BASE_SAVE_PATH). DB 의 images.path 는 이 폴더 기준 경로다.
        ignore_schedule: True 면 스위치와 동작 시간을 무시한다 (python -m app detect --now).
        """
        self.conn = conn
        self.load_pipeline = load_pipeline
        self.writer = writer
        self.image_root = image_root
        self.ignore_schedule = ignore_schedule
        self.now_local = now_local
        self.now_utc = now_utc
        self.pipeline: Pipeline | None = None
        self.consecutive_errors = 0
        self.missing: list[int] = []   # 연속으로 파일이 없던 이미지 ID
        self.processed = 0

    def allowed(self) -> bool:
        return self.ignore_schedule or detection_allowed(repo.get_detector_settings(self.conn), self.now_local())

    def run(self) -> int:
        """할 일이 없거나 조건이 끝날 때까지 처리한다. 처리한 이미지 수를 돌려준다.

        예상 못한 오류가 5장 이어지면 TooManyErrors 를 낸다.
        """
        recovered = repo.recover_detection(self.conn)
        if recovered:
            log.info("끊긴 검출 작업 %d개를 이어서 합니다", recovered)

        while self.allowed():
            job = repo.next_detection_job(self.conn)
            if job is None:
                log.info("검출할 이미지가 없습니다")
                break
            if not self._run_job(job):
                break
        else:
            log.info("검출을 멈춥니다 (꺼짐 또는 동작 시간 밖)")
        return self.processed

    def _run_job(self, job) -> bool:
        """작업 하나를 남은 이미지가 없을 때까지 처리한다. 조건이 끝나 멈췄으면 False."""
        repo.mark_detection_running(self.conn, job["id"], to_db(self.now_utc()))
        log.info("'%s' 검출 시작", job["keyword"])
        while True:
            if not repo.detection_job_exists(self.conn, job["id"]):
                log.info("'%s' 검출 대상에서 빠졌습니다", job["keyword"])
                return True
            if not self.allowed():
                repo.release_detection_job(self.conn, job["id"])
                return False
            image = repo.next_undetected_image(self.conn, job["keyword_id"])
            if image is None:
                status = repo.settle_detection_job(self.conn, job["id"], to_db(self.now_utc()))
                log.info("'%s' 검출 %s", job["keyword"], "완료" if status == "done" else "— 새 이미지를 기다립니다")
                return True
            try:
                self._process(image["id"], self.image_root / image["path"], job["keyword"])
            except TooManyErrors:
                repo.release_detection_job(self.conn, job["id"])
                raise

    def _process(self, image_id: int, path: Path, keyword: str) -> None:
        started = self.now_local()   # 날짜 폴더는 처리를 시작한 날짜로 정한다
        if not path.is_file():
            self._missing(image_id, path)
            return
        self.missing.clear()
        if self.pipeline is None:
            self.pipeline = self.load_pipeline()

        try:
            try:
                result = self.pipeline.run(path)
            except IMAGE_ERRORS as err:   # 그 이미지만의 문제. 다음 장으로 넘어간다
                self._record_error(image_id, path, err)
                return
            saved = self.writer.save(path, keyword, result, started) if self.writer else None
        except Exception as err:          # 모델·GPU·디스크 문제. 이어지면 멈춘다
            log.exception("%s 검출 중 예상 못한 오류", path.name)
            self._record_error(image_id, path, err)
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise TooManyErrors(f"예상 못한 오류가 {MAX_CONSECUTIVE_ERRORS}장 연속 났습니다") from err
            return

        self.consecutive_errors = 0
        self.processed += 1
        repo.save_image_check(self.conn, image_id, check_row(result, saved), detection_rows(result, saved),
                              to_db(self.now_utc()))
        if result.verdict == "positive":
            log.info("양성 %s (%s)", path.name, ", ".join(sorted({r for d in result.detections for r in d.reasons})))

    def _missing(self, image_id: int, path: Path) -> None:
        """파일이 없는 이미지. 한두 장이면 지워진 파일로 보고 error 로 기록하고 넘어간다.

        연속으로 많으면 이미지 폴더 설정(BASE_SAVE_PATH)이 틀렸거나 드라이브가 빠진 것이다.
        그때는 이번에 error 로 적은 기록을 되돌리고 멈춘다. 설정을 고치면 그 이미지부터 다시 검출한다.
        """
        self._record_error(image_id, path, FileNotFoundError(f"파일이 없습니다: {path}"))
        self.missing.append(image_id)
        if len(self.missing) >= MAX_CONSECUTIVE_MISSING:
            repo.clear_image_checks(self.conn, self.missing)
            raise TooManyErrors(
                f"이미지 파일을 {MAX_CONSECUTIVE_MISSING}장 연속 찾지 못했습니다. "
                f"이미지 폴더({self.image_root})가 맞는지 확인하세요 (.env 의 BASE_SAVE_PATH)"
            )

    def _record_error(self, image_id: int, path: Path, err: Exception) -> None:
        log.warning("%s 검출 실패: %s", path.name, err)
        check = {"pipeline": PIPELINE_NAME, "verdict": "error", "error": f"{type(err).__name__}: {err}"[:500]}
        repo.save_image_check(self.conn, image_id, check, [], to_db(self.now_utc()))


def check_row(result: ImageResult, saved) -> dict:
    return {
        "pipeline": PIPELINE_NAME,
        "verdict": result.verdict,
        "section": result.section,
        "yolo_boxes": result.yolo_boxes,
        "skin_passed": len(result.detections),
        "dataset_dir": saved.dataset_dir if saved else None,
        "original_copy": saved.original_copy if saved else None,
        "elapsed_ms": result.elapsed_ms,
    }


def detection_rows(result: ImageResult, saved) -> list[dict]:
    rows = []
    for d in result.detections:
        c = d.candidate
        rows.append({
            "det_index": d.index, "class_id": c.class_id, "yolo_conf": c.confidence,
            "x1": c.x1, "y1": c.y1, "x2": c.x2, "y2": c.y2,
            "swinv2_score": d.swinv2.score, "dinov2_x15_score": d.dinov2_x15.score,
            "dinov2_x2_score": d.dinov2_x2.score,
            "reasons": json.dumps(d.reasons), "positive": int(d.positive),
            "crop_group": saved.crop_groups.get(d.index) if saved else None,
        })
    return rows
