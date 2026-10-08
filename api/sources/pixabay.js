'use strict';

/**
 * Pixabay 사이트 명세 (SourceSpec — 형식은 api/collector.js 참고)
 *
 * API 문서: https://pixabay.com/api/docs/
 *  - 인증  : URL 쿼리 ?key=<API_KEY>
 *  - 한도  : 분당 100회 (시간당 한도보다 이쪽이 먼저 걸림)
 *  - 페이지: total = 전체 검색 수, totalHits = API 로 실제 받을 수 있는 수(보통 500장 상한)
 *            범위를 넘는 페이지를 요청하면 HTTP 400 을 돌려준다
 */

const { fetchWithRetry, HttpError } = require('../lib');

const PER_PAGE = 200; // Pixabay 최대값

module.exports = {
    name: 'pixabay',
    requiredEnv: ['PIXABAY_API_KEY'],
    hourlyLimit: 5000,
    itemDelayMs: 150,

    // 커서 = 페이지 번호
    async fetchPage(page, { config, fetchOpts }) {
        const url = `https://pixabay.com/api/`
            + `?key=${process.env.PIXABAY_API_KEY}&q=${encodeURIComponent(config.keyword)}`
            + `&image_type=photo&page=${page}&per_page=${PER_PAGE}`;
        const res = await fetchWithRetry(url, {}, fetchOpts);
        return res.json();
    },

    // 응답 예: { total, totalHits, hits: [...] }
    // 지금까지 본 개수(page * PER_PAGE)가 totalHits 에 닿으면 마지막 페이지다.
    parsePage: (data, page) => ({
        items: data.hits ?? [],
        total: data.totalHits,
        nextCursor: page * PER_PAGE < (data.totalHits ?? 0) ? page + 1 : null,
    }),

    getId: (photo) => String(photo.id),
    getImageUrl: (photo) => photo.largeImageURL ?? photo.webformatURL,

    // 페이지 범위 초과(400)는 에러가 아니라 "결과 끝" 신호
    isEndOfResults: (err) => err instanceof HttpError && err.status === 400,
};
