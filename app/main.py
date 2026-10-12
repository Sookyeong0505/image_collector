"""웹 서버 (FastAPI).  실행: python -m app serve  →  http://127.0.0.1:8000

서버가 켜지면 스케줄러도 함께 시작해 남아 있던 작업을 바로 이어서 한다.
그래서 PC 가 재부팅된 뒤 서버만 다시 띄우면 수집이 계속된다.

    GET  /                        진행 화면 (HTML 한 장)
    GET  /api/sites               사이트 목록과 키 설정 여부
    POST /api/sites/{name}/check  API 연결 확인
    POST /api/jobs                키워드·사이트 등록 → 수집 시작
    GET  /api/progress            키워드별·사이트별 진행 상황
    POST /api/collector/stop      수집 일시 중단
    POST /api/collector/start     수집 재개
    GET  /api/health              서버 상태 (살아 있는지, 수집 중인지)
    GET  /docs                    API 문서 (자동 생성)

엔드포인트는 모두 async def 로 둔다. 수집 태스크와 같은 스레드에서 실행되므로
SQLite 연결 하나를 잠금 없이 함께 쓸 수 있다.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import repo
from .logging_setup import setup_logging
from .runtime import Runtime, check_source, open_runtime, parse_keywords

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


class JobRequest(BaseModel):
    keywords: str = Field(description="쉼표로 구분한 키워드", examples=["beach, sunset"])
    sites: list[str] = Field(description="수집할 사이트", examples=[["pexels", "naver"]])
    target_count: int | None = Field(default=None, gt=0, description="키워드×사이트당 최대 장수. 비우면 .env 의 MAX_DOWNLOADS")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """settings·transport 는 테스트에서만 넘긴다. 평소에는 .env 를 읽는다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings is None:
            setup_logging()
        async with open_runtime(settings or Settings.from_env(), transport) as runtime:
            app.state.runtime = runtime
            runtime.scheduler.start()
            yield
            await runtime.scheduler.stop()   # 돌던 작업은 pending 으로 돌아가 다음 실행 때 이어진다

    app = FastAPI(title="이미지 수집기", lifespan=lifespan)

    def runtime_of(request: Request) -> Runtime:
        return request.app.state.runtime

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(INDEX_HTML)

    @app.get("/api/sites")
    async def list_sites(request: Request):
        return [
            {"name": name, "configured": source.configured, "hourly_limit": source.hourly_limit}
            for name, source in runtime_of(request).sources.items()
        ]

    @app.post("/api/sites/{name}/check")
    async def check_site(name: str, request: Request):
        runtime = runtime_of(request)
        if name not in runtime.sources:
            raise HTTPException(404, f"알 수 없는 사이트: {name}")
        ok, message = await check_source(runtime.sources[name], runtime.http)
        return {"name": name, "ok": ok, "message": message}

    @app.post("/api/jobs", status_code=201)
    async def create_jobs(body: JobRequest, request: Request):
        runtime = runtime_of(request)
        keywords = parse_keywords(body.keywords)
        if not keywords:
            raise HTTPException(400, "키워드를 하나 이상 입력하세요")
        if not body.sites:
            raise HTTPException(400, "사이트를 하나 이상 선택하세요")

        usable = runtime.scheduler.sources
        unusable = [site for site in body.sites if site not in usable]
        if unusable:
            raise HTTPException(400, f"사용할 수 없는 사이트(키 없음 또는 오타): {', '.join(unusable)}")

        sites = list(dict.fromkeys(body.sites))
        repo.add_keywords(runtime.conn, keywords, sites, body.target_count or runtime.settings.target_count)
        runtime.scheduler.wake()   # 잠든 사이트 태스크를 깨운다
        return {"keywords": keywords, "sites": sites}

    @app.get("/api/progress")
    async def progress(request: Request):
        runtime = runtime_of(request)
        return {"running": runtime.scheduler.running, "keywords": repo.progress(runtime.conn)}

    @app.get("/api/health")
    async def health(request: Request):
        """무인 운영 중 '지금 제대로 돌고 있나'를 한눈에 확인한다."""
        runtime = runtime_of(request)
        return {
            "status": "ok",
            "scheduler_running": runtime.scheduler.running,
            "unfinished_jobs": repo.count_unfinished_jobs(runtime.conn),
            "keep_awake": runtime.scheduler.keep_awake.active,
            "last_saved_at": repo.last_saved_at(runtime.conn),   # UTC
        }

    @app.post("/api/collector/stop")
    async def stop(request: Request):
        await runtime_of(request).scheduler.stop()
        return {"running": False}

    @app.post("/api/collector/start")
    async def start(request: Request):
        runtime_of(request).scheduler.start()
        return {"running": True}

    return app


app = create_app()
