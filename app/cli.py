"""명령줄 실행기.

    python -m app check                               API 키가 유효한지 확인
    python -m app collect "beach, sunset" --sites pexels,naver --target 50
                                                      키워드를 등록하고 끝날 때까지 수집
    python -m app resume                              남은 작업을 이어서 수집
    python -m app status                              진행 상황 출력
    python -m app serve                               웹 서버 실행 (http://127.0.0.1:8000)
"""

import argparse
import asyncio
import os
import sys

from .config import Settings
from .db import repo
from .instance_lock import AlreadyRunning
from .logging_setup import setup_logging
from .runtime import check_source, open_runtime, parse_keywords


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app", description="이미지 자동 수집기")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="키워드를 등록하고 수집한다")
    collect.add_argument("keywords", help='쉼표로 구분한 키워드. 예: "beach, sunset"')
    collect.add_argument("--sites", help="쉼표로 구분한 사이트 (기본: 키가 설정된 모든 사이트)")
    collect.add_argument("--target", type=int, help="키워드×사이트당 최대 장수 (기본: .env 의 MAX_DOWNLOADS)")

    check = commands.add_parser("check", help="API 연결을 확인한다")
    check.add_argument("--sites", help="쉼표로 구분한 사이트 (기본: 전체)")

    commands.add_parser("resume", help="남은 작업을 이어서 수집한다")
    commands.add_parser("status", help="진행 상황을 출력한다")

    serve = commands.add_parser("serve", help="웹 서버를 실행한다")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    # pythonw 로 실행하면 표준 출력이 아예 없다(None). 출력하려는 코드가 죽지 않게 버리는 곳으로 연결한다.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.stdout.reconfigure(errors="replace")

    if args.command == "serve":
        import uvicorn
        # 내 PC 에서만 접속할 수 있게 127.0.0.1 로 연다. 로그는 app.main 이 설정한다.
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
        return

    setup_logging()
    try:
        sys.exit(asyncio.run(_run(args)))
    except AlreadyRunning as err:
        # 수집기가 둘이 되면 같은 작업을 동시에 처리하게 된다. 실행하지 않고 안내만 한다.
        print(f"{err}\n키워드 등록과 진행 확인은 웹 화면에서 하세요. 직접 실행하려면 먼저 돌고 있는 수집기를 멈춰야 합니다.")
        sys.exit(1)
    except KeyboardInterrupt:
        # 돌던 작업은 pending 으로 돌아가 있다. 커서가 저장돼 있어 resume 으로 이어서 할 수 있다.
        print("\n중단했습니다. 'python -m app resume' 으로 이어서 수집할 수 있습니다.")


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()

    # status 와 check 는 들여다보기만 하므로, 수집기가 도는 중에도 실행할 수 있게 잠금 없이 연다
    is_collector = args.command in ("collect", "resume")

    async with open_runtime(settings, collector=is_collector) as runtime:
        if args.command == "status":
            _print_progress(runtime.conn)
            return 0

        if args.command == "check":
            names = _split(args.sites) or list(runtime.sources)
            all_ok = True
            for name in names:
                if name not in runtime.sources:
                    print(f"{name:9} 알 수 없는 사이트")
                    all_ok = False
                    continue
                ok, message = await check_source(runtime.sources[name], runtime.http)
                print(f"{name:9} {'OK  ' if ok else 'FAIL'} {message}")
                all_ok = all_ok and ok
            return 0 if all_ok else 1

        if args.command == "collect":
            keywords = parse_keywords(args.keywords)
            sites = _split(args.sites) or list(runtime.scheduler.sources)
            unusable = [site for site in sites if site not in runtime.scheduler.sources]
            if not keywords or not sites or unusable:
                print(f"키워드와 사용 가능한 사이트가 필요합니다. 사용 가능: {', '.join(runtime.scheduler.sources) or '없음'}"
                      + (f" / 쓸 수 없는 사이트: {', '.join(unusable)}" if unusable else ""))
                return 1
            repo.add_keywords(runtime.conn, keywords, sites, args.target or settings.target_count)

        # collect, resume 공통: 할 일이 없어질 때까지 돌린다
        await runtime.scheduler.run_until_idle()
        _print_progress(runtime.conn)
        return 0


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _print_progress(conn) -> None:
    rows = repo.progress(conn)
    if not rows:
        print("등록된 키워드가 없습니다.")
    for keyword in rows:
        print(f"\n{keyword['keyword']}  [{keyword['status']}]")
        for job in keyword["jobs"]:
            target = job["target_count"] or job["total_available"] or "?"
            line = (f"  {job['site']:9} {job['status']:12} 저장 {job['downloaded']}/{target}"
                    f"  중복 {job['duplicates']}  건너뜀 {job['skipped']}  실패 {job['errors']}")
            if job["resume_at"]:
                line += f"  재개(UTC) {job['resume_at']}"
            if job["last_error"] and job["status"] in ("paused", "failed"):
                line += f"\n            └ {job['last_error']}"
            print(line)
