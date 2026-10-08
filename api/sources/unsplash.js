'use strict';

/**
 * Unsplash 사이트 명세 (SourceSpec — 형식은 api/collector.js 참고)
 *
 * API 문서: https://unsplash.com/documentation
 *  - 인증  : Authorization: Client-ID <ACCESS_KEY> 헤더
 *  - 한도  : Demo 앱 시간당 50회. 검색 호출 + 다운로드 트리거가 모두 포함된다.
 *            이미지 바이트 전송(images.unsplash.com CDN)은 한도에 포함되지 않는다.
 *  - 페이지: 응답의 total_pages 로 마지막 페이지를 안다
 */

const { fetchWithRetry, HttpError } = require('../lib');

const PER_PAGE = 30;

// full = 원본(수 MB, 끊김 잦음) / regular = 1080px. 기본은 regular.
const IMAGE_SIZE = process.env.UNSPLASH_IMAGE_SIZE || 'regular';

// 함수 호출 시점에 읽어야 dotenv 로드 순서와 무관해진다
const authHeader = () => ({ Authorization: `Client-ID ${process.env.UNSPLASH_API_KEY}` });

module.exports = {
    name: 'unsplash',
    requiredEnv: ['UNSPLASH_API_KEY'],
    hourlyLimit: 48,
    itemDelayMs: 200, // CDN 부담 완화

    // 커서 = 페이지 번호
    async fetchPage(page, { config, fetchOpts }) {
        const url = `https://api.unsplash.com/search/photos`
            + `?query=${encodeURIComponent(config.keyword)}&page=${page}&per_page=${PER_PAGE}`;
        const res = await fetchWithRetry(url, { headers: authHeader() }, fetchOpts);
        return res.json();
    },

    // 응답 예: { total, total_pages, results: [...] }
    parsePage: (data, page) => ({
        items: data.results ?? [],
        total: data.total,
        nextCursor: page < (data.total_pages ?? 0) ? page + 1 : null,
    }),

    getId: (photo) => photo.id,
    getImageUrl: (photo) => photo.urls?.[IMAGE_SIZE] ?? photo.urls?.regular,

    // Unsplash API 가이드라인상 "다운로드했다"는 사실을 알려야 한다 (한도에 포함).
    // 트리거가 실패해도 이미 저장된 파일은 유효하므로 에러를 삼킨다.
    async afterSave(photo, { limiter, fetchOpts }) {
        await limiter.take();
        await fetchWithRetry(photo.links.download_location, { headers: authHeader() }, fetchOpts)
            .catch(() => {});
    },

    // 403 = 인증 문제 → 계속 돌려도 의미 없음
    isFatal: (err) => err instanceof HttpError && err.status === 403,
};
