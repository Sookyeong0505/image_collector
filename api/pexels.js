const fs = require('fs');
const path = require('path');
const notifier = require('node-notifier');
require('dotenv').config({ path: path.join(__dirname, '../.env') });

// .env 파일에서 설정값 가져오기
const PEXELS_API_KEY = process.env.PEXELS_API_KEY;
const KEYWORD = process.env.TARGET_KEYWORD;
const BASE_SAVE_PATH = process.env.BASE_SAVE_PATH;

const HOURLY_LIMIT = 200;
const SITE_NAME = 'pexels';

const sanitizedKeyword = KEYWORD.replace(/ /g, '_');
const SAVE_DIR = path.join(BASE_SAVE_PATH, sanitizedKeyword, SITE_NAME);
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function downloadPexels() {
    if (!fs.existsSync(SAVE_DIR)) fs.mkdirSync(SAVE_DIR, { recursive: true });

    const initRes = await fetch(`https://api.pexels.com/v1/search?query=${encodeURIComponent(KEYWORD)}&per_page=1`, { headers: { Authorization: PEXELS_API_KEY }});
    const initData = await initRes.json();
    const totalImages = initData.total_results;

    console.log(`[${SITE_NAME}] 검색어: ${KEYWORD} | 전체 다운로드 대상: ${totalImages}장`);

    let page = 1;
    let downloadedCount = 0;
    let hourlyCount = 0;
    const downloadedIds = new Set();

    while (downloadedCount < totalImages) {
        const res = await fetch(`https://api.pexels.com/v1/search?query=${encodeURIComponent(KEYWORD)}&page=${page}&per_page=80`, { headers: { Authorization: PEXELS_API_KEY }});
        const data = await res.json();

        if (!data.photos || data.photos.length === 0) break;

        for (const photo of data.photos) {
            if (downloadedCount >= totalImages) return;

            if (hourlyCount >= HOURLY_LIMIT) {
                console.log(`[제한 도달] 한도(${HOURLY_LIMIT}장) 초과. 1시간 대기합니다... (${new Date().toLocaleTimeString()})`);
                await sleep(60 * 60 * 1000);
                console.log(`[대기 종료] 다운로드를 재개합니다.`);
                hourlyCount = 0;
            }

            if (downloadedIds.has(photo.id)) continue;

            const savePath = path.join(SAVE_DIR, `${sanitizedKeyword}_${photo.id}.jpg`);
            const imgRes = await fetch(photo.src.original);
            fs.writeFileSync(savePath, Buffer.from(await imgRes.arrayBuffer()));

            downloadedIds.add(photo.id);
            downloadedCount++;
            hourlyCount++;
            console.log(`[${SITE_NAME}] (${downloadedCount}/${totalImages}) 저장 완료`);
        }
        page++;
    }

    console.log(`[종료] 총 ${downloadedCount}장의 이미지 다운로드가 완료되었습니다.`);

    notifier.notify({
        title: '다운로드 완료 알림',
        message: `[${SITE_NAME}] '${KEYWORD}' 이미지 ${downloadedCount}장 저장이 끝났습니다.`,
        sound: true, // 윈도우 기본 알림 소리 재생
        wait: false
    });
}
downloadPexels();