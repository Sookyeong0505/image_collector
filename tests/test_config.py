"""설정 로드: .env 값이 없거나 잘못됐을 때의 기본값."""

from app.config import Settings


def test_MAX_DOWNLOADS를_비우면_2000장():
    assert Settings.from_env({}).target_count == 2000
    assert Settings.from_env({"MAX_DOWNLOADS": ""}).target_count == 2000
    assert Settings.from_env({"MAX_DOWNLOADS": "abc"}).target_count == 2000


def test_MAX_DOWNLOADS를_적으면_그_값():
    assert Settings.from_env({"MAX_DOWNLOADS": "500"}).target_count == 500
