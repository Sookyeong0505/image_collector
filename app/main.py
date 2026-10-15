"""웹 서버 (FastAPI).  실행: python -m app serve  →  http://127.0.0.1:8000

서버가 켜지면 스케줄러도 함께 시작해 남아 있던 작업을 바로 이어서 한다.
그래서 PC 가 재부팅된 뒤 서버만 다시 띄우면 수집이 계속된다.

    GET  /                        진행 화면 (HTML 한 장)
    GET  /api/sites               사이트 목록과 키 설정 여부
    POST /api/sites/{name}/check  API 연결 확인
    POST /api/jobs                키워드·사이트 등록 → 수집 시작
    GET  /api/progress            키워드별·사이트별 진행 상황
    PUT  /api/keywords/order      예약 키워드 순서 바꾸기
    POST /api/keywords/{id}/hold  예약 키워드 보류
    POST /api/keywords/{id}/resume 보류 풀기
    DELETE /api/keywords/{id}     예약 키워드 삭제 (받은 이미지 파일은 남는다)
    POST /api/collector/stop      수집 일시 중단
    POST /api/collector/start     수집 재개
    GET  /api/health              서버 상태 (살아 있는지, 수집 중인지)
    GET  /docs                    API 문서 (자동 생성)

엔드포인트는 모두 async def 로 둔다. 수집 태스크와 같은 스레드에서 실행되므로
SQLite 연결 하나를 잠금 없이 함께 쓸 수 있다.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import repo
from .instance_lock import AlreadyRunning
from .logging_setup import setup_logging
from .runtime import Runtime, check_source, open_runtime, parse_keywords

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


class JobRequest(BaseModel):
    keywords: str = Field(description="쉼표로 구분한 키워드", examples=["beach, sunset"])
    sites: list[str] = Field(description="수집할 사이트", examples=[["pexels", "naver"]])


class OrderRequest(BaseModel):
    ids: list[int] = Field(description="예약 키워드 ID 를 수집할 순서대로", examples=[[3, 1, 2]])


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """settings·transport 는 테스트에서만 넘긴다. 평소에는 .env 를 읽는다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings is None:
            setup_logging()
        try:
            async with open_runtime(settings or Settings.from_env(), transport) as runtime:
                app.state.runtime = runtime
                runtime.scheduler.start()
                yield
                await runtime.scheduler.stop()   # 돌던 작업은 pending 으로 돌아가 다음 실행 때 이어진다
        except AlreadyRunning as err:
            # 콘솔이 없는 실행(자동 시작)에서도 이유를 알 수 있게 로그 파일에 남기고 서버를 띄우지 않는다
            logging.getLogger(__name__).error("서버를 시작하지 않습니다: %s", err)
            raise

    app = FastAPI(title="이미지 수집기", lifespan=lifespan)

    def runtime_of(request: Request) -> Runtime:
        return request.app.state.runtime

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(INDEX_HTML)

    @app.get("/api/sites")
    async def list_sites(request: Request):
        return [
            {
                "name": name, "configured": source.configured,
                "rate_limit": source.rate_limit, "rate_window": source.rate_window,
                # 이번 창에서 더 쓸 수 있는 API 호출 수 (서버 헤더 기준으로 맞춘 값). 한도가 없으면 null
                "calls_left": source.limiter.remaining if source.limiter else None,
            }
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
        repo.add_keywords(runtime.conn, keywords, sites, runtime.settings.target_count)
        runtime.scheduler.wake()   # 잠든 사이트 태스크를 깨운다
        return {"keywords": keywords, "sites": sites}

    @app.get("/api/progress")
    async def progress(request: Request):
        runtime = runtime_of(request)
        return {
            "running": runtime.scheduler.running,
            "max_downloads": runtime.settings.target_count,   # .env 의 MAX_DOWNLOADS
            "keywords": repo.progress(runtime.conn),
        }

    # ── 예약 키워드 관리 ──────────────────────────────────────────

    def queued_keyword(runtime: Runtime, keyword_id: int):
        """수집이 끝나지 않은 키워드만 순서 변경·보류·삭제할 수 있다."""
        keyword = repo.get_keyword(runtime.conn, keyword_id)
        if keyword is None:
            raise HTTPException(404, f"없는 키워드: {keyword_id}")
        if keyword["status"] == "collected":
            raise HTTPException(409, f"이미 수집을 마친 키워드입니다: {keyword['text']}")
        return keyword

    @app.put("/api/keywords/order")
    async def reorder_keywords(body: OrderRequest, request: Request):
        runtime = runtime_of(request)
        ids = list(dict.fromkeys(body.ids))
        if not ids:
            raise HTTPException(400, "키워드 ID 를 하나 이상 보내세요")
        for keyword_id in ids:
            queued_keyword(runtime, keyword_id)
        repo.reorder_keywords(runtime.conn, ids)
        # 지금 수집 중인 작업은 그대로 두고, 각 사이트가 다음 작업을 고를 때부터 새 순서를 따른다
        return {"ids": ids}

    @app.post("/api/keywords/{keyword_id}/hold")
    async def hold_keyword(keyword_id: int, request: Request):
        runtime = runtime_of(request)
        keyword = queued_keyword(runtime, keyword_id)
        repo.set_held(runtime.conn, keyword_id, True)
        await runtime.scheduler.release_keyword(keyword_id)   # 수집 중이었으면 멈추고 다음 키워드로
        logging.getLogger(__name__).info("'%s' 보류", keyword["text"])
        return {"id": keyword_id, "held": True}

    @app.post("/api/keywords/{keyword_id}/resume")
    async def resume_keyword(keyword_id: int, request: Request):
        runtime = runtime_of(request)
        keyword = queued_keyword(runtime, keyword_id)
        repo.set_held(runtime.conn, keyword_id, False)
        runtime.scheduler.wake()
        logging.getLogger(__name__).info("'%s' 보류 해제", keyword["text"])
        return {"id": keyword_id, "held": False}

    @app.delete("/api/keywords/{keyword_id}")
    async def delete_keyword(keyword_id: int, request: Request):
        runtime = runtime_of(request)
        keyword = queued_keyword(runtime, keyword_id)
        repo.set_held(runtime.conn, keyword_id, True)          # 먼저 보류해 다시 집어 들지 않게 한 뒤
        await runtime.scheduler.release_keyword(keyword_id)   # 수집 중인 작업을 멈추고
        repo.delete_keyword(runtime.conn, keyword_id)         # 지운다
        logging.getLogger(__name__).info("'%s' 삭제", keyword["text"])
        return {"id": keyword_id, "deleted": True}

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
