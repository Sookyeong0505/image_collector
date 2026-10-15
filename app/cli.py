"""명령줄 실행기.

    python -m app check                               API 키가 유효한지 확인
    python -m app collect "beach, sunset" --sites pexels,naver
                                                      키워드를 등록하고 끝날 때까지 수집
    python -m app resume                              남은 작업을 이어서 수집
    python -m app status                              진행 상황 출력
    python -m app serve                               웹 서버 실행 (http://127.0.0.1:8000)
    python -m app detect [--now]                      검출 워커 실행 (보통은 serve 가 동작 시간에 띄운다)
    python -m app detect-one <파일 또는 폴더> [--json] 이미지를 검출해 결과만 출력 (검출 의존성 필요)
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

    check = commands.add_parser("check", help="API 연결을 확인한다")
    check.add_argument("--sites", help="쉼표로 구분한 사이트 (기본: 전체)")

    commands.add_parser("resume", help="남은 작업을 이어서 수집한다")
    commands.add_parser("status", help="진행 상황을 출력한다")

    serve = commands.add_parser("serve", help="웹 서버를 실행한다")
    serve.add_argument("--port", type=int, default=8000)

    detect = commands.add_parser("detect", help="검출 워커를 실행한다 (보통은 serve 가 알아서 띄운다)")
    detect.add_argument("--now", action="store_true", help="스위치와 동작 시간을 무시하고 지금 바로 검출한다")

    detect_one = commands.add_parser("detect-one", help="이미지 파일(또는 폴더)을 검출해 결과만 출력한다 (DB·파일 저장 없음)")
    detect_one.add_argument("paths", nargs="+", help="이미지 파일 또는 폴더")
    detect_one.add_argument("--json", action="store_true", help="이미지 1장 = JSON 한 줄로 출력 (reference 와 비교용)")

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

    if args.command == "detect-one":
        sys.exit(_detect_one(args.paths, as_json=args.json))
    if args.command == "detect":
        sys.exit(_detect(ignore_schedule=args.now))

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
            repo.add_keywords(runtime.conn, keywords, sites, settings.target_count)

        # collect, resume 공통: 할 일이 없어질 때까지 돌린다
        await runtime.scheduler.run_until_idle()
        _print_progress(runtime.conn)
        return 0


def _detect(ignore_schedule: bool) -> int:
    """검출 워커 1회 실행. 종료 코드: 0 정상(할 일 없음·조건 끝), 1 오류, 3 다른 워커가 실행 중."""
    import logging

    from .db import connect, init_db
    from .detector.supervisor import EXIT_LOCKED
    from .instance_lock import InstanceLock
    from .keep_awake import KeepAwake

    setup_logging(filename="detector.log")
    log = logging.getLogger("app.detector")
    settings = Settings.from_env()
    lock = InstanceLock(settings.db_path.with_name(settings.db_path.name + ".detect.lock"),
                        "검출 워커가 이미 실행 중입니다.")
    try:
        lock.acquire()
    except AlreadyRunning as err:
        log.info("%s", err)
        return EXIT_LOCKED

    keep_awake = KeepAwake()
    keep_awake.update(True)   # 퇴근 뒤 검출하는 동안 PC 가 절전에 들어가지 않게 한다
    try:
        # torch 등 무거운 패키지는 여기서 처음 불러온다
        from .detector.dataset import DatasetWriter
        from .detector.pipeline import Pipeline
        from .detector.worker import TooManyErrors, Worker

        conn = connect(settings.db_path)
        init_db(conn)
        writer = DatasetWriter(settings.detect_save_path) if settings.detect_save_crops else None
        worker = Worker(conn, lambda: Pipeline.load(settings), writer, settings.base_save_path,
                        ignore_schedule=ignore_schedule)
        try:
            processed = worker.run()
        except TooManyErrors as err:
            log.error("검출을 멈춥니다: %s", err)
            return 1
        except Exception:
            log.exception("검출 워커가 멈췄습니다 (모델 파일, 백본 다운로드 등을 확인하세요)")
            return 1
        log.info("검출 워커 종료: 이번에 %d장 처리", processed)
        return 0
    finally:
        keep_awake.update(False)
        lock.release()


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _detect_one(paths: list[str], as_json: bool) -> int:
    """검출 의존성(torch 등)이 무거워서 이 명령을 쓸 때만 불러온다."""
    import json
    import logging
    from pathlib import Path

    from .detector.pipeline import Pipeline

    logging.basicConfig(level=logging.INFO if not as_json else logging.WARNING, format="%(message)s")
    for noisy in ("httpx", "httpcore", "huggingface_hub"):   # 백본을 받을 때 요청마다 찍는 로그
        logging.getLogger(noisy).setLevel(logging.WARNING)
    files = []
    for path in map(Path, paths):
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        else:
            files.append(path)

    pipeline = Pipeline.load(Settings.from_env())
    failed = 0
    for file in files:
        try:
            result = pipeline.run(file)
        except Exception as err:   # 한 장이 깨져도 나머지는 계속 본다
            failed += 1
            print(json.dumps({"file": file.name, "error": str(err)}, ensure_ascii=False) if as_json
                  else f"{file.name}: 실패 - {err}")
            continue

        if as_json:
            print(json.dumps({
                "file": file.name, "verdict": result.verdict, "section": result.section,
                "yoloBoxes": result.yolo_boxes, "elapsedMs": result.elapsed_ms,
                "detections": [{
                    "detectionId": d.index, "classId": d.candidate.class_id,
                    "yoloConfidence": d.candidate.confidence,
                    "detectionBox": [d.candidate.x1, d.candidate.y1, d.candidate.x2, d.candidate.y2],
                    "swinv2": round(d.swinv2.score, 6), "dinov2": round(d.dinov2_x15.score, 6),
                    "dinov2X2": round(d.dinov2_x2.score, 6), "saveReasons": d.reasons,
                } for d in result.detections],
            }, ensure_ascii=False))
            continue

        print(f"\n{file.name}  →  {result.verdict}  ({result.elapsed_ms}ms, 구역 {result.section or '-'}, "
              f"YOLO 박스 {result.yolo_boxes}, 피부색 통과 {len(result.detections)})")
        for d in result.detections:
            c = d.candidate
            print(f"  #{d.index + 1} class{c.class_id} {c.label:20} YOLO {c.confidence:.4f}  "
                  f"[{c.x1}, {c.y1}, {c.x2}, {c.y2}]")
            print(f"     SwinV2 x1 {d.swinv2.score:.4f}/{d.swinv2.threshold:.2f}  "
                  f"DINOv2 x1.5 {d.dinov2_x15.score:.4f}/{d.dinov2_x15.threshold:.2f}  "
                  f"x2 {d.dinov2_x2.score:.4f}/{d.dinov2_x2.threshold:.2f}  "
                  f"→ {'양성 ' + ', '.join(d.reasons) if d.positive else '음성'}")
    return 1 if failed else 0


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _print_progress(conn) -> None:
    rows = repo.progress(conn)
    if not rows:
        print("등록된 키워드가 없습니다.")
    for keyword in rows:
        held = " · 보류" if keyword["held"] else ""
        print(f"\n{keyword['keyword']}  [{keyword['status']}{held}]")
        for job in keyword["jobs"]:
            done = job["downloaded"] + job["duplicates"] + job["skipped"] + job["errors"]
            total = job["total_available"] if job["total_available"] is not None else "?"
            line = (f"  {job['site']:9} {job['status']:12} 확인 {done}/{total}  저장 {job['downloaded']}"
                    f"  중복 {job['duplicates']}  건너뜀 {job['skipped']}  실패 {job['errors']}")
            if job["resume_at"]:
                line += f"  재개(UTC) {job['resume_at']}"
            if job["last_error"] and job["status"] in ("paused", "failed"):
                line += f"\n            └ {job['last_error']}"
            print(line)
