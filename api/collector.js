'use strict';

/**
 * 수집 엔진 — 모든 사이트가 공유하는 "흐름"만 담당한다.
 *
 * 사이트마다 다른 부분(검색 URL, 응답 모양, 페이지 넘기는 법, 에러 정책)은
 * 엔진이 알지 못한다. 대신 각 사이트가 `SourceSpec` 객체(api/sources/*.js)를 넘겨주면
 * 엔진은 그 안의 함수를 정해진 순서로 호출할 뿐이다.
 *
 *   index.js ──require──▶ sources/pexels.js (spec)
 *       │                         │
 *       └── collect(spec) ◀───────┘
 *               │
 *               ├─ 1. 설정 로드 / 저장 폴더 / 이어받기 목록 준비
 *               ├─ 2. 페이지 루프   : loadPage()    → spec.fetchPage + spec.parsePage
 *               └─ 3. 아이템 루프   : processItem() → spec.getId / getImageUrl → saveImage
 *
 * 페이지 넘기기는 "커서"로 통일한다.
 *   - Unsplash / Pexels / Pixabay : 커서 = 페이지 번호 (1, 2, 3 …)
 *   - Naver                        : 커서 = start offset (1, 101, 201 …)
 * spec.parsePage 가 다음 커서를 돌려주고, 더 없으면 null 을 돌려주면 루프가 끝난다.
 */

const fs = require('fs');
const path = require('path');

const {
    loadConfig, saveImage, scanExisting, HourlyLimiter, run, SkipError, sleep,
} = require('./lib');

/**
 * @typedef {Object} SourceSpec
 * @property {string}   name           저장 폴더명이자 로그 라벨 (예: 'pexels')
 * @property {string[]} requiredEnv    이 사이트에만 필요한 .env 키 (공통 키는 엔진이 추가)
 * @property {number}  [hourlyLimit]   시간당 API 호출 한도. 생략하면 제한 없음
 * @property {*}       [firstCursor=1] 첫 페이지 커서
 *
 * @property {(cursor: *, ctx: Context) => Promise<*>} fetchPage
 *           검색 API 를 호출해 응답 JSON 을 돌려준다.
 * @property {(data: *, cursor: *) => {items: Array, total?: number, nextCursor: *}} parsePage
 *           응답 JSON 에서 아이템 배열 · 전체 개수 · 다음 커서(끝이면 null)를 꺼낸다.
 * @property {(item: *) => string} getId
 *           아이템의 고유 ID. 파일명 `키워드_ID.ext` 에 쓰이며 이어받기의 기준이 된다.
 * @property {(item: *) => (string|undefined)} getImageUrl
 *           내려받을 이미지 URL. 없으면 그 아이템은 건너뛴다.
 *
 * @property {{fetchOptions?: Object, fetchOpts?: Object, minBytes?: number}} [image]
 *           이미지 다운로드에만 적용할 옵션 (saveImage 인자로 그대로 전달)
 * @property {(item: *, ctx: Context) => Promise<void>} [afterSave]
 *           저장 성공 직후 추가로 할 일 (예: Unsplash 다운로드 트리거)
 *
 * @property {(err: Error) => boolean} [isFatal]         true → 실행 전체를 중단
 * @property {(err: Error) => boolean} [isSkippable]     true → '실패'가 아닌 '건너뜀'으로 집계
 * @property {(err: Error) => boolean} [isEndOfResults]  검색 API 에러를 '결과 끝'으로 해석
 *
 * @property {number} [itemDelayMs=0]  이미지 1장 처리 후 쉬는 시간
 * @property {number} [pageDelayMs=0]  검색 페이지 1개 처리 후 쉬는 시간
 */

/**
 * @typedef {Object} Context  spec 의 함수들에 넘겨주는 실행 환경
 * @property {Object}        config     loadConfig() 결과 (keyword, basePath, maxDownloads …)
 * @property {Object}        fetchOpts  fetchWithRetry 의 기본 재시도 옵션
 * @property {HourlyLimiter} limiter    이 사이트의 API 호출 제한기
 */

/**
 * spec 하나를 받아 수집을 실행한다.
 * @param {SourceSpec} spec
 */
function collect(spec) {
    // ── 1. 준비 단계 ─────────────────────────────────────────────
    // 공통 필수값(검색어·저장 경로)은 엔진이 붙여 주므로 spec 은 자기 API 키만 적으면 된다.
    const config = loadConfig([...spec.requiredEnv, 'TARGET_KEYWORD', 'BASE_SAVE_PATH']);

    // 저장 위치: BASE_SAVE_PATH/키워드/사이트명/
    const saveDir = path.join(config.basePath, config.sanitizedKeyword, spec.name);

    const fetchOpts = { retries: config.maxRetries, timeoutMs: config.timeoutMs, label: spec.name };

    // hourlyLimit 이 없으면 Infinity → HourlyLimiter.take() 가 아무것도 하지 않는다.
    const limiter = new HourlyLimiter(spec.hourlyLimit ?? Infinity, `${spec.name} API`);

    const ctx = { config, fetchOpts, limiter };

    // run() 은 stats 를 만들어 넘겨주고, 끝나면(완료/오류/Ctrl+C) 요약과 알림을 처리한다.
    run(spec.name, config.keyword, async (stats) => {
        fs.mkdirSync(saveDir, { recursive: true });

        // 이어받기: 폴더에 이미 있는 파일에서 ID 를 뽑아 둔다. 이 Set 에 있으면 다시 받지 않는다.
        const downloadedIds = scanExisting(saveDir, config.sanitizedKeyword);
        console.log(`[중복 방지] 기존 이미지 ${downloadedIds.size}장 확인 완료.`);

        // ── 2. 페이지 루프 ───────────────────────────────────────
        let cursor = spec.firstCursor ?? 1;
        let isFirstPage = true;

        // nextCursor 가 null 이 되거나 Ctrl+C 로 중단되면 종료
        while (cursor != null && !stats.stopped) {
            const page = await loadPage(spec, cursor, ctx);
            if (!page) break; // isEndOfResults 로 판정된 정상 종료

            if (isFirstPage) {
                console.log(`[${spec.name}] 검색어: ${config.keyword} | 검색 결과 ${page.total ?? '?'}장`);
                isFirstPage = false;
            }

            if (page.items.length === 0) {
                console.log('[안내] 더 이상 검색 결과가 없습니다.');
                break;
            }

            // ── 3. 아이템 루프 ───────────────────────────────────
            for (const item of page.items) {
                if (stats.stopped) break;

                // MAX_DOWNLOADS 에 도달하면 페이지 루프까지 한 번에 빠져나간다.
                if (stats.downloaded >= config.maxDownloads) {
                    console.log('[안내] 이번 실행 목표 장수에 도달했습니다.');
                    return;
                }

                await processItem(spec, item, { ...ctx, saveDir, downloadedIds, stats });
            }

            cursor = page.nextCursor;
            if (cursor == null) {
                console.log('[안내] 마지막 페이지까지 모두 확인했습니다.');
            } else if (spec.pageDelayMs) {
                await sleep(spec.pageDelayMs);
            }
        }
    });
}

/**
 * 검색 페이지 하나를 불러와 { items, total, nextCursor } 로 정리한다.
 * @returns {Promise<{items: Array, total?: number, nextCursor: *} | null>}
 *          null = "결과 끝"으로 해석되는 에러 (예: Pixabay 의 HTTP 400)
 */
async function loadPage(spec, cursor, ctx) {
    try {
        // 검색 API 호출은 시간당 한도에 포함되므로 호출 직전에 허락을 받는다.
        await ctx.limiter.take();
        const data = await spec.fetchPage(cursor, ctx);
        return spec.parsePage(data, cursor);
    } catch (err) {
        if (spec.isEndOfResults?.(err)) {
            console.log('[안내] 페이지 범위 끝에 도달했습니다.');
            return null;
        }
        throw err; // 그 외 에러는 run() 이 받아 '오류 종료' 처리
    }
}

/**
 * 아이템(사진) 하나를 처리한다: 중복 확인 → 다운로드·저장 → 통계 집계.
 *
 *   이미 받음 ──────────▶ skipped++
 *   URL 없음 ───────────▶ skipped++
 *   저장 성공 ──────────▶ downloaded++
 *   저장 실패
 *     ├ isFatal        ─▶ throw (실행 중단: 인증 오류 등)
 *     ├ SkipError /
 *     │ isSkippable    ─▶ skipped++ (깨진 이미지, 핫링크 차단 등)
 *     └ 그 외          ─▶ failed++  (재시도까지 실패한 네트워크 오류 등)
 */
async function processItem(spec, item, ctx) {
    const { config, saveDir, downloadedIds, stats } = ctx;

    const id = spec.getId(item);
    if (downloadedIds.has(id)) {
        stats.skipped++;
        return;
    }

    const url = spec.getImageUrl(item);
    if (!url) {
        stats.skipped++;
        return;
    }

    try {
        // image 옵션이 없는 사이트는 undefined 가 넘어가 saveImage 의 기본값이 적용된다.
        await saveImage({
            url,
            dir: saveDir,
            basename: `${config.sanitizedKeyword}_${id}`, // ← 이어받기가 읽는 파일명 규칙. 바꾸면 안 됨!
            fetchOptions: spec.image?.fetchOptions,
            fetchOpts: spec.image?.fetchOpts ?? ctx.fetchOpts,
            minBytes: spec.image?.minBytes,
        });

        await spec.afterSave?.(item, ctx);

        downloadedIds.add(id);
        stats.downloaded++;
        console.log(`[${spec.name}] (${stats.downloaded}) ${id} 저장 완료`);
    } catch (err) {
        if (spec.isFatal?.(err)) throw err;

        if (err instanceof SkipError || spec.isSkippable?.(err)) {
            stats.skipped++;
            console.log(`  ⤳ 건너뜀 ${id}: ${err.message}`);
        } else {
            stats.failed++;
            console.log(`  ✕ 실패 ${id}: ${err.message}`);
        }
    }

    // 다운로드를 시도한 경우에만 쉰다 (중복으로 건너뛴 아이템은 바로 다음으로).
    if (spec.itemDelayMs) await sleep(spec.itemDelayMs);
}

module.exports = { collect };
