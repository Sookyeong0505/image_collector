'use strict';

/**
 * 네이버 이미지 검색 명세 (SourceSpec — 형식은 api/collector.js 참고)
 *
 * API: NCP API Gateway 의 네이버 검색 (이미지)
 *  - 인증  : X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY 헤더
 *  - 페이지: page 번호가 아니라 start offset 방식 (1, 101, 201 … 최대 1000)
 *  - 특징  : 결과 이미지는 남의 블로그·카페 서버에 있다
 *            → 고유 ID 가 없어서 URL 의 MD5 해시를 ID 로 쓴다
 *            → 핫링크 차단(403)·삭제(404)가 흔해서 실패가 아닌 '건너뜀'으로 센다
 */

const crypto = require('crypto');
const { fetchWithRetry, HttpError } = require('../lib');

const API_BASE = 'https://naverapihub.apigw.ntruss.com/search/v1/image';
const DISPLAY = 100;    // 한 번에 불러올 개수 (최대 100)
const MAX_START = 1000; // start 파라미터 최대값

module.exports = {
    name: 'naver',
    requiredEnv: ['NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'],
    // hourlyLimit 없음 — 대신 페이지마다 0.5초 쉰다
    pageDelayMs: 500,

    // 커서 = start offset
    firstCursor: 1,

    async fetchPage(start, { config, fetchOpts }) {
        const url = `${API_BASE}?query=${encodeURIComponent(config.keyword)}`
            + `&display=${DISPLAY}&start=${start}&filter=large&format=json`;
        const res = await fetchWithRetry(url, {
            headers: {
                'X-NCP-APIGW-API-KEY-ID': process.env.NAVER_CLIENT_ID,
                'X-NCP-APIGW-API-KEY': process.env.NAVER_CLIENT_SECRET,
            },
        }, fetchOpts);
        return res.json();
    },

    // 응답 예: { total, start, display, items: [{ link, thumbnail, ... }] }
    parsePage: (data, start) => ({
        items: data.items ?? [],
        total: data.total,
        nextCursor: start + DISPLAY <= MAX_START ? start + DISPLAY : null,
    }),

    getId: (item) => crypto.createHash('md5').update(item.link).digest('hex'),
    getImageUrl: (item) => item.link,

    // 이미지 다운로드 전용 옵션
    image: {
        // Referer / User-Agent 를 넣어야 핫링크 차단(403)을 상당수 피할 수 있다
        fetchOptions: {
            headers: {
                Referer: 'https://search.naver.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            },
        },
        // 남의 서버라 재시도를 짧게 가져간다
        fetchOpts: { retries: 2, timeoutMs: 30_000, label: 'naver' },
        minBytes: 1024,
    },

    // 4xx(403 핫링크 차단, 404 삭제됨 등)는 흔한 일 → 건너뜀으로 집계
    isSkippable: (err) => err instanceof HttpError && err.status < 500,
};
