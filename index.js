'use strict';

/**
 * 실행 진입점
 *
 *   사용법: node index.js <사이트>      예) node index.js pexels
 *          npm run pexels               (package.json scripts 참고)
 *
 * 흐름: .env 로드 → 사이트 명세(api/sources/<사이트>.js) 로드 → collect(spec) 실행
 */

const fs = require('fs');
const path = require('path');

// sources/*.js 가 모듈 로드 시점에 process.env 를 읽으므로 반드시 가장 먼저 로드한다.
require('dotenv').config({ path: path.join(__dirname, '.env') });

const { collect } = require('./api/collector');

const SOURCES_DIR = path.join(__dirname, 'api', 'sources');
const available = fs.readdirSync(SOURCES_DIR)
    .filter((f) => f.endsWith('.js'))
    .map((f) => path.basename(f, '.js'));

const name = process.argv[2];
if (!available.includes(name)) {
    console.error(`사용법: node index.js <사이트>\n사용 가능한 사이트: ${available.join(', ')}`);
    process.exit(1);
}

collect(require(path.join(SOURCES_DIR, name)));
