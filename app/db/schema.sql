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

-- 키워드 1개의 검출 작업. 수집기가 등록하고 검출 워커가 가져간다 (두 프로세스의 유일한 접점).
CREATE TABLE IF NOT EXISTS detection_jobs (
    id           INTEGER PRIMARY KEY,
    keyword_id   INTEGER NOT NULL UNIQUE REFERENCES keywords(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'done', 'failed')),
    processed    INTEGER NOT NULL DEFAULT 0,
    total        INTEGER,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);
