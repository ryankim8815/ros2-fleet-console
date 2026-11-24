"""SQLite 공용: 연결 열기(파일이 없으면 새로 만듦) + 스키마 버전(PRAGMA user_version) 마이그레이션.

DB 파일(data/places.db, data/alerts.db)은 저장소에 넣지 않습니다. 서버를 켤 때 없으면 만들고,
실행 중에 지워져도 다음 사용 때 ensure() 가 새 파일을 만들어 다시 연결합니다.

마이그레이션은 버전 순서대로 한 번씩만 실행됩니다 (MIGRATIONS[i] 가 버전 i → i+1).
  - 여러 프로세스(serve.py, 실행기·경비 노드)가 같은 DB 를 열어도 BEGIN IMMEDIATE 로 한 곳만 실행
  - 테이블을 다시 만드는 변경을 위해 실행 중에는 외래 키 검사를 끄고, 끝나기 전에 foreign_key_check 로 확인
  - 이미 데이터가 있는 DB 는 바꾸기 전에 <파일>.bak-v<이전 버전> 으로 백업
  - 하나라도 실패하면 그 버전 전체를 되돌리고 예외 (DB 는 이전 버전 그대로)
"""
import os
import sqlite3
import sys


class MigrationError(RuntimeError):
    pass


class Connection(sqlite3.Connection):
    """어떤 파일(장치, inode)에 연결했는지 기억하는 연결 → 같은 경로에 다른 프로세스가 새 파일을 만들어도 알아챔"""
    file_id = None


def _file_id(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def connect(path, migrations):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        # 본 파일 없이 남은 -wal/-shm 은 지운 DB 의 것 → 새 파일에 섞이지 않게 먼저 지움
        for ext in ('-wal', '-shm'):
            try:
                os.remove(path + ext)
            except FileNotFoundError:
                pass
        print(f'DB 파일이 없어 새로 만듭니다: {path}', file=sys.stderr, flush=True)
    con = sqlite3.connect(path, timeout=10, check_same_thread=False, factory=Connection)   # 여러 스레드에서 씀 (잠금은 호출하는 쪽)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    migrate(con, migrations, path)
    con.execute('PRAGMA foreign_keys=ON')
    con.file_id = _file_id(path)
    return con


def ensure(con, path, migrations):
    """연결을 계속 쥐는 쪽(서버·실행기·경비 노드)용: 파일이 지워졌으면 닫고 새 파일로 다시 연결.
    (con, 새로 열었는지) 를 돌려줌. 지워진 파일에 계속 쓰면 데이터가 조용히 사라지므로 쓸 때마다 확인."""
    # 경로만 보면 안 됨: 다른 프로세스(실행기 등)가 먼저 새 파일을 만들었으면 경로는 있지만 우리는 지워진 파일을 쥐고 있음
    if con is not None and getattr(con, 'file_id', None) is not None and con.file_id == _file_id(path):
        return con, False
    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return connect(path, migrations), True


def version(con):
    return con.execute('PRAGMA user_version').fetchone()[0]


def script(con, sql):
    """여러 SQL 문을 하나씩 실행 (executescript 는 진행 중인 트랜잭션을 COMMIT 해 버려 마이그레이션에 못 씀)"""
    buf = ''
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                con.execute(buf)
            buf = ''
    if buf.strip():
        raise MigrationError(f'끝나지 않은 SQL: {buf[:80]}')


def migrate(con, migrations, path=None):
    target = len(migrations)
    if version(con) > target:
        raise MigrationError(f'{path}: DB 버전 {version(con)} 이 이 프로그램({target})보다 새롭습니다')
    if version(con) == target:
        return target
    v0 = version(con)
    has_tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0
    if has_tables and path and not os.path.exists(f'{path}.bak-v{v0}'):
        bak = sqlite3.connect(f'{path}.bak-v{v0}')   # 바꾸기 전 백업 (한 번만)
        con.backup(bak)
        bak.close()
    level = con.isolation_level
    con.isolation_level = None            # BEGIN/COMMIT 을 직접
    con.execute('PRAGMA foreign_keys=OFF')  # 트랜잭션 밖에서만 바꿀 수 있음
    try:
        while True:
            con.execute('BEGIN IMMEDIATE')  # 다른 프로세스가 동시에 마이그레이션하지 못하게 쓰기 잠금
            v = version(con)
            if v >= target:
                con.execute('COMMIT')
                break
            try:
                migrations[v](con)
                bad = con.execute('PRAGMA foreign_key_check').fetchall()
                if bad:
                    raise MigrationError(f'외래 키가 맞지 않는 행 {len(bad)}개: {[tuple(r) for r in bad[:5]]}')
                con.execute(f'PRAGMA user_version = {v + 1}')
                con.execute('COMMIT')
            except Exception as e:
                con.execute('ROLLBACK')
                raise MigrationError(f'{path}: 버전 {v} → {v + 1} 마이그레이션 실패 (DB 는 그대로): {e}') from e
    finally:
        con.isolation_level = level
    return version(con)
