"""DB 파일을 만들고 테이블 목록을 출력한다.  사용법: python -m app.db"""

from . import connect, default_db_path, init_db

conn = connect()
init_db(conn)

tables = [row["name"] for row in conn.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
)]
print(f"DB 준비 완료: {default_db_path()}")
print(f"테이블: {', '.join(tables)}")
conn.close()
