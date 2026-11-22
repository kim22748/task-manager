"""TaskFlow 저장소 — SQLite (Python 표준 라이브러리 sqlite3, 설치 필요 없음).

모든 데이터는 data/taskflow.db 한 파일에 들어갑니다 (첨부 파일의 내용만 data/files/, data/board_files/ 에 둡니다).
예전에는 data/*.json 파일에 저장했습니다. 그 파일이 남아 있으면 서버를 처음 켤 때 한 번 DB 로 옮기고
data/legacy_json/ 으로 치워 둡니다 (지우지 않음).

여러 사용자가 동시에 쓸 때
    - journal_mode=WAL   : 한 사람이 저장하는 동안에도 다른 사람들은 계속 읽는다 (읽기와 쓰기가 서로 막지 않음)
    - busy_timeout       : 동시에 저장하려는 요청은 오류 대신 차례를 기다린다 (쓰기는 한 번에 하나씩)
    - 쓰기는 'BEGIN IMMEDIATE' 트랜잭션: 읽고 → 고치고 → 저장하는 한 묶음이 통째로 반영되거나 통째로 취소된다
    - 서버가 도중에 꺼져도 DB 는 깨지지 않는다 (반쯤 저장된 상태가 남지 않음)

쓰는 법 (server.py · auth_users.py · approval.py · board.py · work_log.py · similar_llm.py 가 씁니다)
    with db.tx:              # 쓰기 트랜잭션. 겹쳐 써도 된다(가장 바깥쪽이 끝날 때 한 번에 저장). 예외가 나면 전부 취소
        items = TASKS.load()
        ...고치기...
        TASKS.save(items)
    with db.read():          # 읽기만: 여러 표를 같은 시점의 내용으로 읽는다
        items = TASKS.load()

표 구조는 아래 SCHEMA 를 보세요. 과제·AI 과제·사용자는 항목마다 컬럼을 두었고, 화면이 쓰는 JSON 모양은 그대로입니다.
"""

import hashlib
import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "taskflow.db"
LEGACY_DIR = DATA_DIR / "legacy_json"      # DB 로 옮긴 예전 JSON 파일을 치워 두는 곳
BUSY_TIMEOUT_MS = 10000                    # 다른 사용자가 저장하는 중이면 이만큼 기다린다
SCHEMA_VERSION = 1

SCHEMA = """
-- 사용자 (로그인 계정). 계정 구분은 사이트관리자(admin) / 일반회원(user) 2종.
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY,
  pos            INTEGER NOT NULL DEFAULT 0,                       -- 목록 순서
  emp_no         TEXT UNIQUE,                                      -- 사번 (6자리 숫자)
  name           TEXT,
  dept           TEXT,
  email          TEXT UNIQUE COLLATE NOCASE,
  role           TEXT CHECK (role IS NULL OR role IN ('admin', 'user')),
  status         TEXT CHECK (status IS NULL OR status IN ('pending', 'active', 'inactive', 'rejected')),
  salt           TEXT,
  pw_hash        TEXT,                                             -- 비밀번호는 PBKDF2 해시로만 저장
  must_change_pw INTEGER CHECK (must_change_pw IS NULL OR must_change_pw IN (0, 1)),
  created_at     TEXT,
  approved_at    TEXT,
  last_login_at  TEXT,
  extra          TEXT,                                             -- 위 컬럼에 없는 항목(JSON)
  sig            TEXT NOT NULL                                     -- 내용 지문: 바뀐 행만 다시 쓰려고 둔다
);

-- 로그인 세션. 토큰 자체가 아니라 SHA-256 해시를 저장한다 (DB 파일이 새도 로그인 상태를 훔칠 수 없게).
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  expires_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- 사용자 관리 처리 이력 (가입·승인·반려·비활성화·비밀번호 초기화 …)
CREATE TABLE IF NOT EXISTS audit_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     TEXT NOT NULL,
  actor  TEXT,
  action TEXT,
  target TEXT,
  detail TEXT
);

-- AI 과제 분류 (사이트 설정 → 분류 관리)
CREATE TABLE IF NOT EXISTS categories (
  name TEXT PRIMARY KEY,
  pos  INTEGER NOT NULL
);

-- AI 과제: 과제의 기준 정보. stage(개발단계)는 연결된 과제 진행의 진행률을 따라간다 (server.py 의 sync_tasks_with_projects).
CREATE TABLE IF NOT EXISTS ai_projects (
  id         INTEGER PRIMARY KEY,
  pos        INTEGER NOT NULL DEFAULT 0,
  title      TEXT,
  purpose    TEXT,
  category   TEXT,
  stage      TEXT,                                                 -- 기획중 / 개발중 / 개발완료
  owner_name TEXT,                                                 -- 과제책임자
  owner_dept TEXT,
  access     TEXT,                                                 -- 공개 / 비공개
  created_at TEXT,
  updated_at TEXT,
  extra      TEXT,
  sig        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_projects_owner ON ai_projects(owner_name);
CREATE INDEX IF NOT EXISTS idx_ai_projects_category ON ai_projects(category);

-- 과제 진행: AI 과제와 1:1 (project_id). 값이 NULL 인 컬럼은 '그 항목이 아직 없음'을 뜻한다.
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY,
  pos           INTEGER NOT NULL DEFAULT 0,
  project_id    INTEGER,                                           -- 연결된 AI 과제
  title         TEXT,
  s             TEXT,                                              -- 예전 진행 구분(todo/doing/done). 화면은 개발단계·활용상태를 쓴다
  date          TEXT,                                              -- 등록일
  assignee      TEXT,                                              -- 과제책임자 이름
  due           TEXT,                                              -- 마감일
  priority      TEXT,
  memo          TEXT,
  progress      INTEGER CHECK (progress IS NULL OR progress BETWEEN 0 AND 100),
  pub_requested INTEGER CHECK (pub_requested IS NULL OR pub_requested IN (0, 1)),   -- 게시 요청 (과제책임자)
  registered    INTEGER CHECK (registered IS NULL OR registered IN (0, 1)),         -- 서비스 게시 (사이트관리자)
  approver      TEXT,
  reg_reason    TEXT,
  registered_at TEXT,
  use_ended     INTEGER CHECK (use_ended IS NULL OR use_ended IN (0, 1)),           -- 활용 종료 (사이트관리자)
  extra         TEXT,
  sig           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);

-- 과제의 변경 이력 / 첨부 파일 정보 / 과제참여자 (과제를 지우면 함께 지워진다)
CREATE TABLE IF NOT EXISTS task_history (
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq     INTEGER NOT NULL,
  at      TEXT,
  text    TEXT,
  by      TEXT,                                                    -- 작업한 사람 이름
  file    TEXT,                                                    -- 첨부 이력이면 그 파일 정보(JSON)
  extra   TEXT,
  PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS task_files (
  task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  file_id     TEXT,                                                -- 파일 내용은 data/files/<과제id>/<file_id>
  name        TEXT,
  size        INTEGER,
  kind        TEXT,                                                -- 파일 형식 (확장자 대문자 또는 '기타')
  note        TEXT,
  uploaded_at TEXT,
  extra       TEXT,
  PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS task_participants (
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq     INTEGER NOT NULL,
  emp_no  TEXT,
  name    TEXT,
  dept    TEXT,
  extra   TEXT,
  PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_participants_emp ON task_participants(emp_no);

-- 신규 과제 등록 신청 (일반회원이 신청 → 사이트관리자 승인). 신청 내용(fields)은 extra 에 JSON 으로 둔다.
CREATE TABLE IF NOT EXISTS project_requests (
  id               INTEGER PRIMARY KEY,
  pos              INTEGER NOT NULL DEFAULT 0,
  status           TEXT,                                           -- pending / rejected
  requester_emp_no TEXT,
  requester_name   TEXT,
  requested_at     TEXT,
  extra            TEXT,
  sig              TEXT NOT NULL
);

-- 공유 게시판 글. 본문·첨부 목록은 extra 에 JSON 으로 둔다 (첨부 내용은 data/board_files/<글id>/).
CREATE TABLE IF NOT EXISTS board_posts (
  id            INTEGER PRIMARY KEY,
  pos           INTEGER NOT NULL DEFAULT 0,
  task_id       INTEGER,
  title         TEXT,
  author_emp_no TEXT,
  created_at    TEXT,
  extra         TEXT,
  sig           TEXT NOT NULL
);

-- 작업 기록 (과제 삭제 · 엑셀 올리기 · 분류 변경 …). 기록 한 건 전체를 doc 에 JSON 으로 둔다.
CREATE TABLE IF NOT EXISTS work_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     TEXT NOT NULL,
  actor  TEXT,
  action TEXT,
  doc    TEXT NOT NULL
);

-- 사이트 설정 (AI 서비스 연결 정보 등). 값은 JSON.
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 내부 표시: 어떤 데이터를 처음 채웠는지 등
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 과제별 개발단계·활용상태 (보고서·점검용. 화면은 storage.js 의 TaskState 가 같은 기준으로 계산한다)
DROP VIEW IF EXISTS v_task_state;
CREATE VIEW v_task_state AS
  SELECT id AS task_id, project_id, title, assignee, COALESCE(progress, 0) AS progress,
         CASE WHEN COALESCE(progress, 0) >= 100 THEN '개발완료' WHEN COALESCE(progress, 0) > 0 THEN '개발중' ELSE '기획중' END AS dev_stage,
         CASE WHEN COALESCE(use_ended, 0) = 1 THEN '활용종료' WHEN COALESCE(registered, 0) = 1 THEN '게시활용중'
              WHEN COALESCE(progress, 0) < 100 THEN '개발진행중' WHEN COALESCE(pub_requested, 0) = 1 THEN '게시검토중' ELSE '신청대기중' END AS use_status
  FROM tasks;
"""


def connect(path=None):
    """여러 사용자가 동시에 쓰는 것을 전제로 한 연결 (맨 위 설명 참고)."""
    c = sqlite3.connect(path or DB_FILE, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)   # 트랜잭션은 직접 관리
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


# ---------------------------------------------------------------- 트랜잭션

_local = threading.local()       # 스레드(요청)마다: 열려 있는 연결, 겹친 깊이, 쓰기인지
_writer = threading.RLock()      # 같은 서버 안의 쓰기 요청을 차례로 세운다 (다른 프로세스와는 SQLite 잠금으로 맞춘다)


class _Tx:
    """쓰기 트랜잭션. 예전 저장소의 lock 자리에 그대로 쓴다: `with store.lock:` 한 묶음이 트랜잭션 하나가 된다.

    겹쳐 쓸 수 있다(같은 요청 안에서 여러 저장소가 서로를 부를 때). 가장 바깥쪽이 끝날 때 한 번에 저장하고,
    예외가 가장 바깥쪽까지 올라오면 그 요청이 바꾼 것을 모두 취소한다.
    """

    def __enter__(self):
        depth = getattr(_local, "depth", 0)
        if depth == 0:
            _writer.acquire()
            try:
                con = connect()
                con.execute("BEGIN IMMEDIATE")
            except BaseException:
                _writer.release()
                raise
            _local.con, _local.write = con, True
        elif not getattr(_local, "write", False):
            raise RuntimeError("읽기 전용 구간(db.read) 안에서는 쓰기 트랜잭션을 시작할 수 없습니다.")
        _local.depth = depth + 1
        return _local.con

    def __exit__(self, exc_type, exc, tb):
        _local.depth -= 1
        if _local.depth == 0:
            con = _local.con
            _local.con, _local.write = None, False
            try:
                con.execute("ROLLBACK" if exc_type is not None else "COMMIT")
            finally:
                con.close()
                _writer.release()
        return False


tx = _Tx()


@contextmanager
def read():
    """읽기 전용 구간: 여러 표를 같은 시점의 내용으로 읽는다. 쓰기 트랜잭션 안에서 부르면 그 트랜잭션의 연결을 쓴다."""
    if getattr(_local, "depth", 0) > 0:
        yield _local.con
        return
    con = connect()
    con.execute("BEGIN")
    _local.con, _local.depth, _local.write = con, 1, False
    try:
        yield con
    finally:
        _local.con, _local.depth = None, 0
        try:
            con.execute("COMMIT")
        finally:
            con.close()


def _con():
    con = getattr(_local, "con", None) if getattr(_local, "depth", 0) > 0 else None
    if con is None:
        raise RuntimeError("db.tx 또는 db.read() 안에서만 쓸 수 있습니다.")
    return con


# ---------------------------------------------------------------- 표 ↔ 화면이 쓰는 JSON

def _dumps(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _to_db(value, kind):
    if value is None:
        return None
    if kind == "bool":
        return int(bool(value))
    if kind == "json":
        return _dumps(value)
    return value


def _from_db(value, kind):
    if kind == "bool":
        return bool(value)
    if kind == "json":
        return json.loads(value)
    return value


class Child:
    """항목에 딸린 목록 (예: 과제의 변경 이력). key 는 JSON 에서의 이름. always=True 면 비어 있어도 [] 로 내보낸다."""

    def __init__(self, key, table, fields, always=False):
        self.key, self.table, self.fields, self.always = key, table, fields, always
        self.cols = [col for _, col, _ in fields]

    def load(self, con):
        out = {}
        for r in con.execute(f"SELECT * FROM {self.table} ORDER BY task_id, seq"):
            d = {}
            for key, col, kind in self.fields:
                if r[col] is not None:
                    d[key] = _from_db(r[col], kind)
            if r["extra"]:
                d.update(json.loads(r["extra"]))
            out.setdefault(r["task_id"], []).append(d)
        return out

    def replace(self, con, parent_id, rows):
        con.execute(f"DELETE FROM {self.table} WHERE task_id = ?", (parent_id,))
        known = {key for key, _, _ in self.fields}
        sql = f"INSERT INTO {self.table} (task_id, seq, {', '.join(self.cols)}, extra) VALUES (?, ?, {', '.join('?' * len(self.cols))}, ?)"
        for seq, row in enumerate(rows or []):
            row = row if isinstance(row, dict) else {"value": row}
            extra = {k: v for k, v in row.items() if k not in known}
            con.execute(sql, [parent_id, seq] + [_to_db(row.get(key), kind) for key, _, kind in self.fields] + [_dumps(extra) if extra else None])


class Entity:
    """목록 하나(과제, AI 과제, 사용자 …)를 표에 저장한다. 화면·서버가 쓰는 dict 목록 모양과 순서를 그대로 지킨다.

    fields: (JSON 이름, 컬럼, 종류) 목록. 종류는 int / text / bool / json.
    컬럼이 NULL 이면 그 항목이 없는 것으로 읽는다. fields 에 없는 항목은 extra 컬럼에 JSON 으로 보관한다(하나도 잃지 않음).
    save() 는 내용이 바뀐 행만 다시 쓴다 (행마다 내용 지문 sig 를 두고 비교).
    """

    def __init__(self, table, fields, children=()):
        self.table, self.fields, self.children = table, fields, children
        self.cols = [col for _, col, _ in fields]

    def load(self):
        con = _con()
        kids = [(ch, ch.load(con)) for ch in self.children]
        out = []
        for r in con.execute(f"SELECT * FROM {self.table} ORDER BY pos, id"):
            d = {"id": r["id"]}
            for key, col, kind in self.fields:
                if r[col] is not None:
                    d[key] = _from_db(r[col], kind)
            if r["extra"]:
                d.update(json.loads(r["extra"]))
            for ch, rows in kids:
                if r["id"] in rows or ch.always:
                    d[ch.key] = rows.get(r["id"], [])
            out.append(d)
        return out

    def save(self, items):
        con = _con()
        cur = {r["id"]: (r["sig"], r["pos"]) for r in con.execute(f"SELECT id, sig, pos FROM {self.table}")}
        known = {key for key, _, _ in self.fields} | {ch.key for ch in self.children} | {"id"}
        seen = set()
        upsert = (f"INSERT INTO {self.table} (id, pos, {', '.join(self.cols)}, extra, sig) VALUES (?, ?, {', '.join('?' * len(self.cols))}, ?, ?) "
                  f"ON CONFLICT(id) DO UPDATE SET pos = excluded.pos, {', '.join(f'{c} = excluded.{c}' for c in self.cols)}, extra = excluded.extra, sig = excluded.sig")
        for pos, it in enumerate(items):
            iid = it["id"]
            if iid in seen:
                continue
            seen.add(iid)
            sig = hashlib.sha1(_dumps(it).encode("utf-8")).hexdigest()
            old = cur.get(iid)
            if old is None or old[0] != sig:
                extra = {k: v for k, v in it.items() if k not in known}
                con.execute(upsert, [iid, pos] + [_to_db(it.get(key), kind) for key, _, kind in self.fields] + [_dumps(extra) if extra else None, sig])
                for ch in self.children:
                    ch.replace(con, iid, it.get(ch.key))
            elif old[1] != pos:
                con.execute(f"UPDATE {self.table} SET pos = ? WHERE id = ?", (pos, iid))
        gone = [(i,) for i in cur if i not in seen]
        if gone:
            con.executemany(f"DELETE FROM {self.table} WHERE id = ?", gone)   # 딸린 이력·첨부·참여자는 ON DELETE CASCADE


USERS = Entity("users", [
    ("empNo", "emp_no", "text"), ("name", "name", "text"), ("dept", "dept", "text"), ("email", "email", "text"),
    ("role", "role", "text"), ("status", "status", "text"), ("salt", "salt", "text"), ("pwHash", "pw_hash", "text"),
    ("mustChangePw", "must_change_pw", "bool"), ("createdAt", "created_at", "text"), ("approvedAt", "approved_at", "text"),
    ("lastLoginAt", "last_login_at", "text"),
])
AI_PROJECTS = Entity("ai_projects", [
    ("title", "title", "text"), ("purpose", "purpose", "text"), ("category", "category", "text"), ("stage", "stage", "text"),
    ("ownerName", "owner_name", "text"), ("ownerDept", "owner_dept", "text"), ("access", "access", "text"),
    ("createdAt", "created_at", "text"), ("updatedAt", "updated_at", "text"),
])
TASKS = Entity("tasks", [
    ("projectId", "project_id", "int"), ("title", "title", "text"), ("s", "s", "text"), ("date", "date", "text"),
    ("assignee", "assignee", "text"), ("due", "due", "text"), ("priority", "priority", "text"), ("memo", "memo", "text"),
    ("progress", "progress", "int"), ("pubRequested", "pub_requested", "bool"), ("registered", "registered", "bool"),
    ("approver", "approver", "text"), ("regReason", "reg_reason", "text"), ("registeredAt", "registered_at", "text"),
    ("useEnded", "use_ended", "bool"),
], children=(
    Child("history", "task_history", [("at", "at", "text"), ("text", "text", "text"), ("by", "by", "text"), ("file", "file", "json")], always=True),
    Child("files", "task_files", [("id", "file_id", "text"), ("name", "name", "text"), ("size", "size", "int"), ("kind", "kind", "text"),
                                 ("note", "note", "text"), ("uploadedAt", "uploaded_at", "text")]),
    Child("participants", "task_participants", [("empNo", "emp_no", "text"), ("name", "name", "text"), ("dept", "dept", "text")]),
))
PROJECT_REQUESTS = Entity("project_requests", [
    ("status", "status", "text"), ("requesterEmpNo", "requester_emp_no", "text"), ("requesterName", "requester_name", "text"),
    ("requestedAt", "requested_at", "text"),
])
BOARD_POSTS = Entity("board_posts", [
    ("taskId", "task_id", "int"), ("title", "title", "text"), ("authorEmpNo", "author_emp_no", "text"), ("createdAt", "created_at", "text"),
])


# ---------------------------------------------------------------- 분류 · 설정 · 기록

def categories_load():
    return [r["name"] for r in _con().execute("SELECT name FROM categories ORDER BY pos")]


def categories_save(names):
    con = _con()
    con.execute("DELETE FROM categories")
    con.executemany("INSERT OR IGNORE INTO categories (name, pos) VALUES (?, ?)", [(str(n), i) for i, n in enumerate(names)])


def setting_get(key, default=None):
    with read() as con:
        r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def setting_set(key, value):
    with tx as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, _dumps(value)))


def setting_delete(key):
    with tx as con:
        con.execute("DELETE FROM settings WHERE key = ?", (key,))


def audit_add(rec):
    with tx as con:
        con.execute("INSERT INTO audit_log (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                    (rec.get("at", ""), rec.get("actor", ""), rec.get("action", ""), rec.get("target", ""), rec.get("detail", "")))


def audit_read(limit=200):
    with read() as con:
        return [dict(r) for r in con.execute("SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]


def work_log_add(rec):
    with tx as con:
        con.execute("INSERT INTO work_log (at, actor, action, doc) VALUES (?, ?, ?, ?)", (rec.get("at", ""), rec.get("actor", ""), rec.get("action", ""), _dumps(rec)))


def work_log_read(limit=500):
    with read() as con:
        return [json.loads(r["doc"]) for r in con.execute("SELECT doc FROM work_log ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- 로그인 세션

def _token_hash(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def session_get(token):
    """{"uid", "exp"} 또는 None."""
    if not token:
        return None
    with read() as con:
        r = con.execute("SELECT user_id, expires_at FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
    return {"uid": r["user_id"], "exp": r["expires_at"]} if r else None


def session_put(token, user_id, expires_at, now):
    with tx as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))      # 지난 세션은 이때 함께 치운다
        con.execute("INSERT OR REPLACE INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)", (_token_hash(token), user_id, expires_at))


def session_drop(token):
    if token:
        with tx as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def session_drop_user(user_id, keep_token=None):
    """그 사용자의 로그인을 모두 끊는다 (keep_token 은 남긴다: 비밀번호를 바꾼 그 기기)."""
    with tx as con:
        con.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?", (user_id, _token_hash(keep_token) if keep_token else ""))


# ---------------------------------------------------------------- 처음 채우기 · 예전 JSON 옮기기

def first_time(name):
    """이 이름으로 처음 불렸을 때만 True (표를 처음 채울 때 한 번만 실행하려고). db.tx 안에서 부른다."""
    con = _con()
    if con.execute("SELECT 1 FROM meta WHERE key = ?", ("seeded:" + name,)).fetchone():
        return False
    con.execute("INSERT INTO meta (key, value) VALUES (?, '1')", ("seeded:" + name,))
    return True


def legacy_json(path, want=list):
    """예전 JSON 파일의 내용. 없거나 읽을 수 없으면 None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, want) else None


def legacy_lines(path):
    """예전 기록 파일(한 줄에 JSON 하나)의 내용 목록. 없으면 []."""
    out = []
    try:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    except (FileNotFoundError, OSError):
        pass
    return out


def retire(path):
    """DB 로 옮긴 예전 파일을 data/legacy_json/ 으로 치운다 (지우지 않는다. 치우지 못해도 서버는 계속 돈다)."""
    path = Path(path)
    try:
        if path.exists():
            LEGACY_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(LEGACY_DIR / path.name))
    except OSError:
        pass


def adopt(name, path, save, load=legacy_json, default=None):
    """표를 처음 채울 때 한 번만: 예전 파일(path)이 있으면 그 내용을, 없으면 default() 를 save 로 저장한다.
    옮긴 예전 파일은 data/legacy_json/ 으로 치운다. 이미 채운 적이 있으면 아무것도 하지 않는다."""
    with tx:
        if not first_time(name):
            return False
        old = load(path)
        data = old if old is not None else (default() if default else None)
        if data:
            save(data)
    if old is not None:
        retire(path)
    return True


def backup(dest):
    """서버가 켜져 있어도 안전하게 DB 전체를 다른 파일로 복사한다 (SQLite 온라인 백업)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, dst = connect(), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        con.close()


init()
