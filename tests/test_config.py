"""설정 로드: .env 값이 없거나 잘못됐을 때의 기본값."""

from app.config import Settings


def test_MAX_DOWNLOADS를_비우면_2000장():
    assert Settings.from_env({}).target_count == 2000
    assert Settings.from_env({"MAX_DOWNLOADS": ""}).target_count == 2000
    assert Settings.from_env({"MAX_DOWNLOADS": "abc"}).target_count == 2000


def test_MAX_DOWNLOADS를_적으면_그_값():
    assert Settings.from_env({"MAX_DOWNLOADS": "500"}).target_count == 500


def test_검출_설정_기본값():
    settings = Settings.from_env({})
    assert settings.detect_save_crops is True
    assert settings.detect_device == "auto"
    assert settings.model_dir.name == "models" and settings.detect_save_path.name == "detect"


def test_DETECT_SAVE_CROPS는_참거짓_글자를_읽고_모르는_값이면_기본값():
    assert Settings.from_env({"DETECT_SAVE_CROPS": "false"}).detect_save_crops is False
    assert Settings.from_env({"DETECT_SAVE_CROPS": "OFF"}).detect_save_crops is False
    assert Settings.from_env({"DETECT_SAVE_CROPS": "1"}).detect_save_crops is True
    assert Settings.from_env({"DETECT_SAVE_CROPS": "아마도"}).detect_save_crops is True
