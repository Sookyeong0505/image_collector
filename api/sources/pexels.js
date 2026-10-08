'use strict';

/**
 * Pexels 사이트 명세 (SourceSpec — 형식은 api/collector.js 참고)
 *
 * API 문서: https://www.pexels.com/api/documentation/
 *  - 인증  : Authorization: <API_KEY> 헤더
 *  - 한도  : 시간당 200회
 *  - 페이지: 응답에 next_page 가 있으면 다음 페이지가 있다
 */

const { fetchWithRetry, HttpError } = require('../lib');

const PER_PAGE = 80; // Pexels 최대값

// original(원본, 매우 큼) / large2x(1880px) / large(940px)
const IMAGE_SIZE = process.env.PEXELS_IMAGE_SIZE || 'large2x';

module.exports = {
    name: 'pexels',
    requiredEnv: ['PEXELS_API_KEY'],
    hourlyLimit: 190, // 공식 한도 200 — 여유분 확보
    itemDelayMs: 150,

    // 커서 = 페이지 번호
    async fetchPage(page, { config, fetchOpts }) {
        const url = `https://api.pexels.com/v1/search`
            + `?query=${encodeURIComponent(config.keyword)}&page=${page}&per_page=${PER_PAGE}`;
        const res = await fetchWithRetry(url, {
            headers: { Authorization: process.env.PEXELS_API_KEY },
        }, fetchOpts);
        return res.json();
    },

    // 응답 예: { total_results, page, photos: [...], next_page: "https://..." }
    parsePage: (data, page) => ({
        items: data.photos ?? [],
        total: data.total_results,
        nextCursor: data.next_page ? page + 1 : null,
    }),

    getId: (photo) => String(photo.id),
    getImageUrl: (photo) => photo.src?.[IMAGE_SIZE] ?? photo.src?.original,

    // 401 = API 키 문제 → 계속 돌려도 결과가 같으므로 중단
    isFatal: (err) => err instanceof HttpError && err.status === 401,
};
