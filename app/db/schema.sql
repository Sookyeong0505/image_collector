-- 이미지 수집 파이프라인 스키마 (설계 설명: docs/COLLECTION_SPEC.md)
--
-- 모든 문장이 IF NOT EXISTS 라서 앱을 켤 때마다 실행해도 안전하다.
-- 시각은 UTC 문자열('2026-10-12 08:30:00')로 저장한다. SQLite 에는 날짜 전용 타입이 없다.

-- 사용자가 입력한 키워드 1개 = 1행
CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY,
    text        TEXT    NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL,              -- 입력한 순서. 사이트는 이 순서대로 키워드를 처리한다
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'collecting', 'collected')),
    held        INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0, 1)),  -- 1 = 보류. 스케줄러가 이 키워드의 작업을 건너뛴다
    detect_hidden INTEGER NOT NULL DEFAULT 0 CHECK (detect_hidden IN (0, 1)),  -- 1 = 검출 '키워드 추가' 목록에서 숨김
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 키워드 × 사이트 수집 작업 1개 = 1행
CREATE TABLE IF NOT EXISTS collect_jobs (
    id              INTEGER PRIMARY KEY,
    keyword_id      INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    site            TEXT    NOT NULL,          -- 'unsplash' | 'pexels' | 'pixabay' | 'naver'
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'rate_limited', 'paused', 'done', 'failed')),

    -- 다음에 요청할 검색 위치(책갈피). 페이지 번호나 start 오프셋을 문자열로 담는다.
    -- NULL 이면 처음부터. 페이지 하나를 다 처리한 뒤에만 갱신한다.
    cursor          TEXT,

    target_count    INTEGER,                   -- 이 작업에서 받을 최대 장수. NULL = 가능한 만큼
    total_available INTEGER,                   -- 사이트가 알려준 검색 결과 수 (진행률 분모)

    -- 진행 카운터
    downloaded      INTEGER NOT NULL DEFAULT 0, -- 새로 저장한 장수
    duplicates      INTEGER NOT NULL DEFAULT 0, -- 중복이라 저장하지 않은 장수
    skipped         INTEGER NOT NULL DEFAULT 0, -- 깨진 이미지 등으로 건너뛴 장수
    errors          INTEGER NOT NULL DEFAULT 0, -- 재시도까지 실패한 장수

    auth_failures   INTEGER NOT NULL DEFAULT 0, -- 인증 오류 횟수. 3이 되면 failed
    resume_at       TEXT,                       -- rate_limited / paused 일 때 다시 시작할 시각
    last_error      TEXT,
    started_at      TEXT,
    finished_at     TEXT,

    UNIQUE (keyword_id, site)
);

-- 스케줄러가 "이 사이트에서 다음에 할 작업"을 찾을 때 쓴다
CREATE INDEX IF NOT EXISTS idx_collect_jobs_site_status ON collect_jobs (site, status);

-- 고유한 이미지 1장 = 1행
-- path 가 NULL 인 행은 "지각 해시가 비슷해서 저장하지 않은 중복"의 기록이다.
CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT    NOT NULL UNIQUE,     -- 파일 바이트의 해시 (L1: 완전히 같은 파일)
    phash         TEXT    NOT NULL,            -- 64비트 지각 해시, 16진수 16자 (L2: 리사이즈·재압축본)
    path          TEXT,                        -- 저장한 파일 경로
    duplicate_of  INTEGER REFERENCES images(id),
    width         INTEGER,
    height        INTEGER,
    bytes         INTEGER,
    detected_at   TEXT,                        -- 검출 워커가 처리한 시각. NULL = 아직 검출 전
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),

    -- 파일이 있으면 원본, 없으면 반드시 어떤 원본의 중복이어야 한다
    CHECK ((path IS NULL) = (duplicate_of IS NOT NULL))
);

-- "이 이미지를 어느 사이트의 어떤 키워드 검색에서 발견했나"
-- 같은 이미지가 여러 사이트·키워드에서 나올 수 있으므로 images 와 분리한다.
CREATE TABLE IF NOT EXISTS image_sources (
    image_id    INTEGER NOT NULL REFERENCES images(id)   ON DELETE CASCADE,
    keyword_id  INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    site        TEXT    NOT NULL,
    source_id   TEXT    NOT NULL,              -- 사이트가 준 사진 ID (네이버는 URL 의 MD5)
    source_url  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),

    -- (site, source_id) 로 시작하므로 L0 검사("이 사이트의 이 ID 를 본 적 있나?")가 이 인덱스를 탄다
    PRIMARY KEY (site, source_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS idx_image_sources_image   ON image_sources (image_id);
CREATE INDEX IF NOT EXISTS idx_image_sources_keyword ON image_sources (keyword_id);

-- 사용자가 검출하라고 고른 키워드 1개 = 1행 (설계: docs/DETECTION_SPEC.md 5-1)
-- 수집기와 검출 워커의 접점. 화면이 등록하고 워커가 가져간다.
--   pending  검출할 이미지가 있다 (또는 아직 확인 전)
--   running  워커가 처리 중
--   waiting  남은 이미지가 없지만 키워드가 아직 수집 중이다. 새 이미지가 들어오면 다시 처리한다
--   done     키워드 수집도 끝났고 모든 이미지를 검출했다
CREATE TABLE IF NOT EXISTS detection_jobs (
    id           INTEGER PRIMARY KEY,
    keyword_id   INTEGER NOT NULL UNIQUE REFERENCES keywords(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'waiting', 'done')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);

-- 자동 검출 설정. 항상 1행만 있다. 시각은 PC 현지 시각 'HH:MM'.
-- 시작이 끝보다 늦으면 자정을 넘는 구간이고(19:00~08:00), 같으면 하루 종일이다.
CREATE TABLE IF NOT EXISTS detector_settings (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    enabled       INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    window_start  TEXT    NOT NULL DEFAULT '19:00',
    window_end    TEXT    NOT NULL DEFAULT '08:00'
);
INSERT OR IGNORE INTO detector_settings (id) VALUES (1);

-- 이미지 1장의 검출 요약. 다시 검출하면 덮어쓴다.
CREATE TABLE IF NOT EXISTS image_checks (
    image_id      INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    pipeline      TEXT    NOT NULL,              -- 'original'. 개선 브랜치 결과와 구분하는 이름
    verdict       TEXT    NOT NULL CHECK (verdict IN ('positive', 'negative', 'error')),
    section       TEXT    CHECK (section IN ('full', 'left', 'right')),  -- 채택한 구역. YOLO 박스가 없으면 NULL
    yolo_boxes    INTEGER NOT NULL DEFAULT 0,    -- 채택 구역의 박스 수 (NMS 후)
    skin_passed   INTEGER NOT NULL DEFAULT 0,    -- 피부색 필터 통과 수 = detections 행 수
    dataset_dir   TEXT,                          -- 결과 파일을 저장한 날짜 폴더 (예: 20261015_dataset). 저장 안 했으면 NULL
    original_copy TEXT,                          -- 원본 사본 경로 (날짜 폴더 기준)
    error         TEXT,
    elapsed_ms    INTEGER,
    checked_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 피부색 필터를 통과한 검출 1건 = 1행
CREATE TABLE IF NOT EXISTS detections (
    id               INTEGER PRIMARY KEY,
    image_id         INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    det_index        INTEGER NOT NULL,           -- 이미지 안에서의 순번 (0부터)
    class_id         INTEGER NOT NULL CHECK (class_id IN (1, 3, 4, 5)),
    yolo_conf        REAL    NOT NULL,
    x1 INTEGER NOT NULL, y1 INTEGER NOT NULL, x2 INTEGER NOT NULL, y2 INTEGER NOT NULL,  -- 원본 좌표
    swinv2_score     REAL    NOT NULL,           -- x1 Crop
    dinov2_x15_score REAL    NOT NULL,           -- x1.5 Crop
    dinov2_x2_score  REAL    NOT NULL,           -- x2 Crop
    reasons          TEXT    NOT NULL,           -- 참인 조건 이름의 JSON 배열. 비어 있으면 음성
    positive         INTEGER NOT NULL CHECK (positive IN (0, 1)),
    crop_group       TEXT,                       -- 저장한 Crop 3장의 그룹 ID. 저장 안 했으면 NULL
    UNIQUE (image_id, det_index)
);
