'use strict';

/**
 * image_collector 공통 유틸리티
 *  - fetchWithRetry : 네트워크 오류 / 429 / 5xx 자동 재시도
 *  - scanExisting   : 이미 저장된 파일 스캔 (이어받기)
 *  - saveImage      : 상태코드 + 용량 + 매직바이트 검증 후 원자적 저장
 *  - HourlyLimiter  : 남은 시간만큼만 대기하는 시간당 호출 제한기
 *  - run            : 예외 / Ctrl+C 를 잡아 요약 출력 + 알림
 */

const fs = require('fs');
const path = require('path');
const notifier = require('node-notifier');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ------------------------------------------------------------------ */
/* 에러 타입                                                           */
/* ------------------------------------------------------------------ */

/** HTTP 응답은 받았으나 2xx가 아닌 경우 */
class HttpError extends Error {
    constructor(status, url, bodySnippet = '') {
        super(`HTTP ${status} — ${url}`);
        this.name = 'HttpError';
        this.status = status;
        this.bodySnippet = bodySnippet;
    }
}

/** 이 항목만 건너뛰면 되는 경우 (깨진 이미지, 핫링크 차단 등) */
class SkipError extends Error {
    constructor(message) {
        super(message);
        this.name = 'SkipError';
    }
}

/* ------------------------------------------------------------------ */
/* 설정 로딩                                                           */
/* ------------------------------------------------------------------ */

/**
 * .env 값을 읽고 필수값이 비어 있으면 즉시 종료한다.
 * @param {string[]} requiredKeys 반드시 채워져 있어야 하는 키 목록
 */
function loadConfig(requiredKeys) {
    const missing = requiredKeys.filter((key) => !process.env[key]);
    if (missing.length > 0) {
        console.error(`[설정 오류] .env 에 다음 값이 비어 있습니다: ${missing.join(', ')}`);
        process.exit(1);
    }

    const keyword = process.env.TARGET_KEYWORD;
    return {
        keyword,
        sanitizedKeyword: keyword.replace(/[\\/:*?"<>|\s]+/g, '_'),
        basePath: process.env.BASE_SAVE_PATH,
        // 선택 항목 — 없으면 기본값
        maxDownloads: toInt(process.env.MAX_DOWNLOADS, Infinity),
        timeoutMs: toInt(process.env.REQUEST_TIMEOUT_MS, 60_000),
        maxRetries: toInt(process.env.MAX_RETRIES, 4),
    };
}

function toInt(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
}

/* ------------------------------------------------------------------ */
/* 재시도 fetch                                                        */
/* ------------------------------------------------------------------ */

const RETRYABLE_CODES = new Set([
    'ECONNRESET', 'ECONNREFUSED', 'ECONNABORTED', 'ETIMEDOUT',
    'EPIPE', 'EAI_AGAIN', 'ENOTFOUND', 'EHOSTUNREACH', 'ENETUNREACH',
    'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT',
    'UND_ERR_SOCKET',
]);

function isRetryableNetworkError(err) {
    if (err?.name === 'AbortError' || err?.name === 'TimeoutError') return true;
    const codes = [err?.code, err?.cause?.code, err?.cause?.cause?.code];
    return codes.some((c) => c && RETRYABLE_CODES.has(c));
}

function isRetryableStatus(status) {
    return status === 408 || status === 425 || status === 429 || status >= 500;
}

/**
 * 네트워크 오류·429·5xx 에 대해 지수 백오프로 재시도하는 fetch.
 * 4xx(429 제외)는 재시도해도 소용없으므로 즉시 HttpError 를 던진다.
 */
async function fetchWithRetry(url, options = {}, { retries = 4, timeoutMs = 60_000, label = '' } = {}) {
    let lastError;

    for (let attempt = 0; attempt <= retries; attempt++) {
        try {
            const res = await fetch(url, {
                ...options,
                signal: AbortSignal.timeout(timeoutMs),
            });

            if (res.ok) return res;

            // 재시도해도 결과가 같은 4xx → 바로 실패 처리
            if (!isRetryableStatus(res.status)) {
                const body = await res.text().catch(() => '');
                throw new HttpError(res.status, url, body.slice(0, 300));
            }

            // 429 는 서버가 알려준 대기시간을 우선 존중
            const retryAfter = parseInt(res.headers.get('retry-after') || '', 10);
            const wait = Number.isFinite(retryAfter)
                ? retryAfter * 1000
                : backoffDelay(attempt);

            if (attempt === retries) {
                throw new HttpError(res.status, url);
            }
            console.log(`  ↻ ${label || url} — HTTP ${res.status}, ${Math.round(wait / 1000)}초 후 재시도 (${attempt + 1}/${retries})`);
            await sleep(wait);
            continue;
        } catch (err) {
            if (err instanceof HttpError) throw err;

            lastError = err;
            if (!isRetryableNetworkError(err) || attempt === retries) break;

            const wait = backoffDelay(attempt);
            const code = err?.cause?.code || err?.code || err?.name || 'unknown';
            console.log(`  ↻ ${label || url} — ${code}, ${Math.round(wait / 1000)}초 후 재시도 (${attempt + 1}/${retries})`);
            await sleep(wait);
        }
    }

    throw lastError ?? new Error(`요청 실패: ${url}`);
}

/** 1s → 2s → 4s → 8s (최대 30s) + 지터 */
function backoffDelay(attempt) {
    const base = Math.min(1000 * 2 ** attempt, 30_000);
    return base + Math.floor(Math.random() * 500);
}

/* ------------------------------------------------------------------ */
/* 파일 저장 / 이어받기                                                */
/* ------------------------------------------------------------------ */

const MAGIC = [
    { ext: '.jpg', bytes: [0xFF, 0xD8, 0xFF] },
    { ext: '.png', bytes: [0x89, 0x50, 0x4E, 0x47] },
    { ext: '.gif', bytes: [0x47, 0x49, 0x46, 0x38] },
    { ext: '.bmp', bytes: [0x42, 0x4D] },
];

/** 실제 바이트로 이미지 형식 판별 (확장자·content-type 은 자주 틀림) */
function sniffExtension(buffer) {
    for (const { ext, bytes } of MAGIC) {
        if (bytes.every((b, i) => buffer[i] === b)) return ext;
    }
    // WEBP: "RIFF"...."WEBP"
    if (buffer.slice(0, 4).toString('ascii') === 'RIFF' &&
        buffer.slice(8, 12).toString('ascii') === 'WEBP') return '.webp';
    return null;
}

function extensionFromUrl(url) {
    const m = url.split('?')[0].match(/\.(jpe?g|png|gif|webp|bmp)$/i);
    if (!m) return null;
    const ext = m[1].toLowerCase();
    return ext === 'jpeg' ? '.jpg' : `.${ext}`;
}

/**
 * 이미지를 내려받아 검증 후 저장한다.
 * 임시파일(.part)에 쓴 뒤 rename 하므로 중간에 죽어도 깨진 파일이 남지 않는다.
 * @returns {string} 저장된 파일 경로
 */
async function saveImage({ url, dir, basename, fetchOptions = {}, fetchOpts = {}, minBytes = 2048 }) {
    const res = await fetchWithRetry(url, fetchOptions, fetchOpts);
    const contentType = (res.headers.get('content-type') || '').toLowerCase();
    const buffer = Buffer.from(await res.arrayBuffer());

    if (buffer.length < minBytes) {
        throw new SkipError(`용량 미달 (${buffer.length}B) — 에러 페이지 가능성`);
    }

    const sniffed = sniffExtension(buffer);
    if (!sniffed && !contentType.startsWith('image/')) {
        throw new SkipError(`이미지가 아님 (content-type: ${contentType || '없음'})`);
    }

    const ext = sniffed ?? extensionFromUrl(url) ?? '.jpg';
    const finalPath = path.join(dir, `${basename}${ext}`);
    const tempPath = `${finalPath}.part`;

    fs.writeFileSync(tempPath, buffer);
    fs.renameSync(tempPath, finalPath);
    return finalPath;
}

/**
 * 저장 폴더를 스캔해 이미 받은 ID 집합을 만든다. (이어받기 핵심)
 * 파일명 규칙: `${prefix}_${id}.${ext}`
 */
function scanExisting(dir, prefix) {
    const ids = new Set();
    if (!fs.existsSync(dir)) return ids;

    for (const file of fs.readdirSync(dir)) {
        // 이전 실행에서 남은 미완성 파일 정리
        if (file.endsWith('.part')) {
            try { fs.unlinkSync(path.join(dir, file)); } catch { /* 무시 */ }
            continue;
        }
        if (!file.startsWith(`${prefix}_`)) continue;

        const id = file.slice(prefix.length + 1).replace(/\.[a-z0-9]+$/i, '');
        if (id) ids.add(id);
    }
    return ids;
}

/* ------------------------------------------------------------------ */
/* 시간당 호출 제한                                                    */
/* ------------------------------------------------------------------ */

/**
 * 시간당 N회 제한을 관리한다.
 * 기존 코드는 한도 도달 시 무조건 1시간을 쉬었지만,
 * 이미 지난 시간을 빼고 "남은 시간"만 대기하므로 훨씬 빠르다.
 */
class HourlyLimiter {
    constructor(limit, label = '') {
        this.limit = limit;
        this.label = label;
        this.count = 0;
        this.windowStart = Date.now();
    }

    /** API 호출 직전에 반드시 await 할 것 */
    async take() {
        if (!Number.isFinite(this.limit) || this.limit <= 0) return;

        const WINDOW = 60 * 60 * 1000;
        const elapsed = Date.now() - this.windowStart;

        if (elapsed >= WINDOW) {
            this.count = 0;
            this.windowStart = Date.now();
        }

        if (this.count >= this.limit) {
            const wait = WINDOW - (Date.now() - this.windowStart) + 5_000;
            const resumeAt = new Date(Date.now() + wait).toLocaleTimeString();
            console.log(`[제한 도달] ${this.label} 한도(${this.limit}회) 소진. ${Math.ceil(wait / 60000)}분 대기 → ${resumeAt} 재개`);
            await sleep(wait);
            this.count = 0;
            this.windowStart = Date.now();
            console.log(`[대기 종료] 다운로드를 재개합니다. (${new Date().toLocaleTimeString()})`);
        }

        this.count++;
    }
}

/* ------------------------------------------------------------------ */
/* 실행 래퍼                                                           */
/* ------------------------------------------------------------------ */

/**
 * main 함수를 감싸 예외·Ctrl+C 를 처리하고 종료 시 요약과 알림을 낸다.
 * main 은 진행 상황을 담은 stats 객체를 인자로 받는다.
 */
function run(siteName, keyword, main) {
    const stats = { downloaded: 0, skipped: 0, failed: 0, stopped: false };
    let finished = false;

    const finish = (reason) => {
        if (finished) return;
        finished = true;

        console.log(`\n[${reason}] ${siteName} — 저장 ${stats.downloaded}장 / 건너뜀 ${stats.skipped}장 / 실패 ${stats.failed}장`);
        notifier.notify({
            title: `이미지 수집 ${reason}`,
            message: `[${siteName}] '${keyword}' — ${stats.downloaded}장 저장 완료`,
            sound: true,
            wait: false,
        });
    };

    process.on('SIGINT', () => {
        stats.stopped = true;
        console.log('\n[중단 요청] 현재 작업을 정리하고 종료합니다...');
        finish('중단');
        setTimeout(() => process.exit(0), 500);
    });

    main(stats)
        .then(() => finish('완료'))
        .catch((err) => {
            console.error(`\n[치명적 오류] ${err?.message ?? err}`);
            if (err?.bodySnippet) console.error(err.bodySnippet);
            if (err?.stack) console.error(err.stack.split('\n').slice(1, 4).join('\n'));
            finish('오류 종료');
            process.exitCode = 1;
        });
}

module.exports = {
    sleep,
    loadConfig,
    fetchWithRetry,
    saveImage,
    scanExisting,
    HourlyLimiter,
    run,
    HttpError,
    SkipError,
};
