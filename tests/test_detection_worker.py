"""검출 워커 1회 실행과 날짜 폴더 저장 (명세 5-3 ~ 5-5). 실제 모델 대신 가짜 파이프라인을 쓴다."""

import json
from datetime import datetime

import pytest

pytest.importorskip("torch")   # 검출 의존성(.[detect])이 없으면 건너뛴다
from PIL import Image

from app.db import repo
from app.detector.dataset import DatasetWriter, dataset_dir_name
from app.detector.pipeline import Detection, ImageResult
from app.detector.skin import Candidate
from app.detector.verifiers import HeadScore
from app.detector.worker import TooManyErrors, Worker
from tests.test_detection_jobs import add_keyword, job

EVENING = datetime(2026, 10, 15, 20, 0)
PASS, FAIL = HeadScore(0.9, 0.5), HeadScore(0.1, 0.5)


def positive_result(confidence=0.7) -> ImageResult:
    crop = Image.new("RGB", (8, 8), (200, 150, 120))
    detection = Detection(
        0, Candidate(1, "EXPOSED_BREAST_F", confidence, "full", 1, 2, 9, 10),
        {"x1": (1, 2, 9, 10), "x1.5": (0, 0, 10, 12), "x2": (0, 0, 12, 14)},
        {"x1": crop, "x1.5": crop, "x2": crop}, FAIL, PASS, FAIL,
    )
    return ImageResult("full", 1, [detection], elapsed_ms=12)


NEGATIVE = ImageResult(None, 0, [], elapsed_ms=5)


class FakePipeline:
    """파일 이름에 'pos' 가 있으면 양성, 'broken' 이면 이미지 오류, 'crash' 면 예상 못한 오류."""

    def __init__(self):
        self.seen = []

    def run(self, path):
        self.seen.append(path.name)
        if "broken" in path.name:
            raise OSError("이미지를 읽을 수 없습니다")
        if "crash" in path.name:
            raise RuntimeError("CUDA out of memory")
        return positive_result() if "pos" in path.name else NEGATIVE


def add_file_image(conn, keyword_id, folder, name, *, create=True) -> int:
    """수집기처럼 이미지 폴더(BASE_SAVE_PATH) 기준 상대 경로 '키워드/사이트/파일' 로 기록한다."""
    relative = f"beach/pexels/{name}"
    if create:
        (folder / "beach" / "pexels").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16)).save(folder / relative)
    image_id = repo.insert_image(conn, sha256=f"sha-{name}", phash="0" * 16, path=relative,
                                 duplicate_of=None, width=16, height=16, size=1)
    repo.link_source(conn, image_id, keyword_id, "pexels", name, None)
    return image_id


@pytest.fixture
def images(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    return folder


def make_worker(conn, tmp_path, pipeline=None, *, clock=None, ignore_schedule=False):
    loads = []
    pipeline = pipeline or FakePipeline()

    def load():
        loads.append(1)
        return pipeline
    worker = Worker(conn, load, DatasetWriter(tmp_path / "detect"), tmp_path / "images",
                    ignore_schedule=ignore_schedule,
                    now_local=clock or (lambda: EVENING), now_utc=lambda: EVENING)
    worker.loads, worker.fake = loads, pipeline
    return worker


def test_고른_키워드의_이미지를_모두_검출하고_작업을_마친다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    first = add_file_image(conn, keyword_id, images, "a_pos.jpg")
    add_file_image(conn, keyword_id, images, "b.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    worker = make_worker(conn, tmp_path)
    assert worker.run() == 2

    assert worker.fake.seen == ["a_pos.jpg", "b.jpg"]
    assert job(conn, keyword_id)["status"] == "done"
    check = conn.execute("SELECT * FROM image_checks WHERE image_id = ?", (first,)).fetchone()
    assert (check["verdict"], check["dataset_dir"], check["section"]) == ("positive", "20261015_dataset", "full")
    detection = conn.execute("SELECT * FROM detections").fetchone()
    assert json.loads(detection["reasons"]) == ["yolo_confidence", "dinov2_threshold_x1.5"]
    assert detection["crop_group"] and detection["positive"] == 1


def test_꺼져_있거나_동작_시간_밖이면_아무것도_하지_않는다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    add_file_image(conn, keyword_id, images, "a.jpg")
    repo.select_for_detection(conn, keyword_id)

    worker = make_worker(conn, tmp_path)                       # 꺼짐
    assert worker.run() == 0 and worker.loads == []
    repo.update_detector_settings(conn, enabled=True)
    worker = make_worker(conn, tmp_path, clock=lambda: datetime(2026, 10, 15, 9, 0))   # 근무 시간
    assert worker.run() == 0 and worker.loads == []
    assert make_worker(conn, tmp_path, ignore_schedule=True).run() == 1                  # --now


def test_동작_시간이_끝나면_지금_이미지까지만_하고_작업을_돌려놓는다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        add_file_image(conn, keyword_id, images, name)
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)
    moments = iter([EVENING, EVENING, EVENING, datetime(2026, 10, 16, 8, 0)])   # 두 번째 장 뒤에 08:00

    worker = make_worker(conn, tmp_path, clock=lambda: next(moments, datetime(2026, 10, 16, 8, 0)))
    assert worker.run() == 1
    assert job(conn, keyword_id)["status"] == "pending"
    assert repo.next_undetected_image(conn, keyword_id)["path"].endswith("b.jpg")


def test_작업_중에_키워드를_빼면_멈춘다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    add_file_image(conn, keyword_id, images, "a.jpg")
    add_file_image(conn, keyword_id, images, "b.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    pipeline = FakePipeline()
    original = pipeline.run

    def run_and_unselect(path):
        repo.unselect_for_detection(conn, keyword_id)
        return original(path)
    pipeline.run = run_and_unselect
    assert make_worker(conn, tmp_path, pipeline).run() == 1


def test_수집_중인_키워드는_남은_이미지가_없으면_기다린다(conn, tmp_path, images):
    keyword_id = add_keyword(conn, status="collecting")
    add_file_image(conn, keyword_id, images, "a.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)
    make_worker(conn, tmp_path).run()
    assert job(conn, keyword_id)["status"] == "waiting"


def test_깨진_이미지는_error로_기록하고_계속한다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    broken = add_file_image(conn, keyword_id, images, "broken.jpg")
    add_file_image(conn, keyword_id, images, "ok.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    assert make_worker(conn, tmp_path).run() == 1
    check = conn.execute("SELECT * FROM image_checks WHERE image_id = ?", (broken,)).fetchone()
    assert check["verdict"] == "error" and "OSError" in check["error"]
    assert job(conn, keyword_id)["status"] == "done"


def test_지워진_파일_한두_장은_error로_기록하고_계속한다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    gone = add_file_image(conn, keyword_id, images, "gone.jpg", create=False)
    add_file_image(conn, keyword_id, images, "ok.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    assert make_worker(conn, tmp_path).run() == 1
    check = conn.execute("SELECT * FROM image_checks WHERE image_id = ?", (gone,)).fetchone()
    assert check["verdict"] == "error" and "파일이 없습니다" in check["error"]


def test_파일이_20장_연속_없으면_폴더_설정_문제로_보고_기록을_되돌리고_멈춘다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    for index in range(25):
        add_file_image(conn, keyword_id, images, f"gone{index}.jpg", create=False)
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    with pytest.raises(TooManyErrors, match="BASE_SAVE_PATH"):
        make_worker(conn, tmp_path).run()
    assert conn.execute("SELECT COUNT(*) FROM image_checks").fetchone()[0] == 0          # 기록을 남기지 않는다
    assert conn.execute("SELECT COUNT(*) FROM images WHERE detected_at IS NULL").fetchone()[0] == 25
    assert job(conn, keyword_id)["status"] == "pending"


def test_예상_못한_오류가_5장_이어지면_멈춘다(conn, tmp_path, images):
    keyword_id = add_keyword(conn)
    for index in range(6):
        add_file_image(conn, keyword_id, images, f"crash{index}.jpg")
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)

    with pytest.raises(TooManyErrors):
        make_worker(conn, tmp_path).run()
    assert job(conn, keyword_id)["status"] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM image_checks WHERE verdict = 'error'").fetchone()[0] == 5


def test_할_일_없이_정리만_할_때는_모델을_올리지_않는다(conn, tmp_path):
    keyword_id = add_keyword(conn)
    repo.select_for_detection(conn, keyword_id)
    repo.update_detector_settings(conn, enabled=True)
    worker = make_worker(conn, tmp_path)
    worker.run()
    assert worker.loads == [] and job(conn, keyword_id)["status"] == "done"


# ── 날짜 폴더 저장 ──────────────────────────────────────────────────


def test_양성이면_날짜_폴더에_원본_Crop_metadata를_저장한다(tmp_path, images):
    source = images / "beach 1.jpg"
    Image.new("RGB", (16, 16)).save(source)
    writer = DatasetWriter(tmp_path / "detect")

    saved = writer.save(source, "beach", positive_result(0.83125), datetime(2026, 10, 15, 23, 59, 59))
    folder = tmp_path / "detect" / "20261015_dataset"
    assert saved.dataset_dir == "20261015_dataset"
    assert (folder / saved.original_copy).read_bytes() == source.read_bytes()
    crops = sorted(p.name for p in (folder / "class1").iterdir())
    group = saved.crop_groups[0]
    assert crops == sorted(f"beach_1__det01__class1__{s}__yolo0.8313__{group}.jpg" for s in ("x1", "x1.5", "x2"))
    assert not (folder / "class3").exists()                  # 검출이 없는 클래스 폴더는 만들지 않는다

    [line] = (folder / "metadata.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert (record["sourceFile"], record["keyword"], record["classId"]) == ("beach 1.jpg", "beach", 1)
    assert record["savedFiles"]["x2"] == f"class1/beach_1__det01__class1__x2__yolo0.8313__{group}.jpg"
    assert record["modelVotes"] == {"swinv2_x1": False, "dinov2_x1.5": True, "dinov2_x2": False}
    assert record["detectedImageFile"] == saved.original_copy


def test_음성이면_아무것도_저장하지_않는다(tmp_path, images):
    source = images / "a.jpg"
    Image.new("RGB", (16, 16)).save(source)
    assert DatasetWriter(tmp_path / "detect").save(source, "beach", NEGATIVE, EVENING) is None
    assert not (tmp_path / "detect").exists()


def test_날짜_폴더_이름은_검출을_시작한_현지_날짜():
    assert dataset_dir_name(datetime(2026, 1, 2, 0, 0, 1)) == "20260102_dataset"
