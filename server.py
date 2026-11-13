"""과제 관리 서버 — 과제를 data/tasks.json 파일에 영구 저장합니다.

외부 라이브러리 없이 Python 표준 라이브러리만 사용합니다.

실행 (Python 3.7 이상):
    python3 server.py              # http://localhost:8885
    python3 server.py --port 9000
    python3 server.py --reset-demo # data/ 의 과제·AI 과제·사용자를 demo-data.json 더미데이터로 되돌림

로그인 (A01, auth_users.py):
    아래 과제 API 는 모두 로그인이 필요합니다. 조회는 모든 역할, 등록·수정·삭제·첨부는
    사이트관리자(admin)·과제책임자(owner)만, 서비스 게시(registered) 변경은 사이트관리자만 할 수 있습니다.
    로그인·사용자 관리 API 는 auth_users.py 맨 위 설명을 보세요.
    data/ 파일이 없으면 demo-data.json 의 더미데이터(과제 10건, AI 과제 10건, 사용자 10명)로 채웁니다.

API:
    GET    /api/tasks          과제 목록
    POST   /api/tasks          과제 생성   {"title": "...", "assignee", "due": "YYYY-MM-DD", "priority": "high|mid|low", "memo"}
    PATCH  /api/tasks/<id>     과제 수정   {"s": "todo|doing|done", "title", "assignee", "due", "priority", "memo", "progress": 0~100}
                               (변경 내용은 history 에 자동 기록됩니다)
    DELETE /api/tasks/<id>     과제 삭제
    POST   /api/tasks/import   여러 과제 한 번에 가져오기 (localStorage 이전용)

    GET    /api/tasks/<id>/files          첨부 파일 목록
    POST   /api/tasks/<id>/files?name=..  첨부 파일 올리기 (요청 본문 = 파일 내용, 20MB 이하)
    GET    /api/tasks/<id>/files/<fid>    첨부 파일 내려받기
    DELETE /api/tasks/<id>/files/<fid>    첨부 파일 삭제
                               (파일은 data/files/<과제id>/ 에, 목록은 과제의 files 항목에 저장)

    GET    /api/ai-projects        AI 과제 목록 (data/ai_projects.json)
    POST   /api/ai-projects        AI 과제 등록
    PUT    /api/ai-projects/<id>   AI 과제 수정 (등록 때와 같은 항목 전체)
    DELETE /api/ai-projects/<id>   AI 과제 삭제
    GET    /api/ai-projects/export        (사이트관리자) AI 과제 목록을 엑셀(.xlsx)로 내려받기
    GET    /api/ai-projects/template      (사이트관리자) 엑셀 올리기 양식 내려받기
    POST   /api/ai-projects/import[?dry=1] (사이트관리자) 엑셀로 한 번에 등록 (요청 본문 = .xlsx 파일, 5MB·1000행 이하)
                                   dry=1 이면 검사 결과만 돌려주고 저장하지 않음. 한 행이라도 틀리면 아무것도 저장하지 않음
    POST   /api/ai-projects/bulk-delete   (사이트관리자) 선택한 AI 과제 한 번에 삭제 {"ids": [...]}
    GET    /api/work-log                  (사이트관리자) AI 과제 삭제·엑셀 올리기 작업 기록 (최근 500건)
                                   AI 과제는 명칭+과제책임자가 같으면 중복으로 보고 등록·수정·올리기를 막습니다 (409)

    /api/llm/*, /api/similar/group   유사과제 탐색 AI(LLM) 창구 — similar_llm.py 맨 위 설명 참고
"""

import argparse
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import similar_llm  # 유사과제 탐색 AI(LLM) 창구 (A04)
import work_log  # AI 과제 삭제·엑셀 올리기 작업 기록 (data/work_log.jsonl)
import xlsx_util  # 엑셀(.xlsx) 쓰기·읽기 (표준 라이브러리만)
from auth_users import DEFAULT_SEED_PASSWORD, DEMO_FILE, AuthMixin, ValidationError, users_store

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "tasks.json"
FILES_DIR = BASE_DIR / "data" / "files"
MAX_FILE = 20 * 1024 * 1024  # 첨부 파일 1개 최대 크기 (20MB)
FILE_ROUTE = re.compile(r"^/api/tasks/(\d+)/files(?:/([0-9a-f]{32}))?$")
# PRD 필수 첨부 3종: 확장자로 종류를 정한다 (내용은 보지 않음)
FILE_KINDS = {".hwpx": "과제기획서", ".html": "과제실행 파일", ".htm": "과제실행 파일", ".csv": "과제활용 데이터"}
AI_DATA_FILE = BASE_DIR / "data" / "ai_projects.json"
AI_CATEGORIES = ["개발", "법률", "노사", "IT"]
AI_STAGES = ["기획중", "개발중", "승인요청", "승인검토중", "활용중", "활용종료"]
AI_ACCESS = ["전사", "소속부서", "사용자별 지정"]
# 엑셀 열: (항목, 머리글, 같은 뜻으로 받아 주는 머리글들, 열 너비)
AI_COLUMNS = [
    ("title", "명칭", ("과제명", "AI 과제명"), 30),
    ("purpose", "목적", (), 60),
    ("category", "분류", (), 10),
    ("stage", "단계", (), 12),
    ("ownerName", "과제책임자", ("과제책임자 이름", "책임자"), 14),
    ("ownerDept", "소속부서", ("과제책임자 소속부서", "부서"), 16),
    ("access", "접근권한범위", ("접근권한",), 14),
]
MAX_XLSX = 5 * 1024 * 1024   # 올리는 엑셀 파일 최대 크기
MAX_IMPORT_ROWS = 1000
STATUSES = {"todo", "doing", "done"}
PRIORITIES = {"high", "mid", "low"}
MAX_TITLE = 200
MAX_HISTORY = 100

# 변경 이력에 쓰는 화면 이름 (storage.js 의 TRACK 과 같은 형식)
STAGE_LABEL = {"todo": "검토", "doing": "진행", "done": "종료"}
PRIORITY_LABEL = {"high": "높음", "mid": "보통", "low": "낮음"}
TRACKED = [
    ("s", "단계", lambda v: STAGE_LABEL.get(v, v)),
    ("progress", "진행률", lambda v: f"{v}%"),
    ("registered", "서비스 게시", lambda v: "게시" if v else "해제"),
    ("approver", "승인자", lambda v: v or "없음"),
    ("regReason", "게시 사유", lambda v: v or "없음"),
    ("assignee", "담당자", lambda v: v or "없음"),
    ("due", "마감일", lambda v: v or "없음"),
    ("priority", "우선순위", lambda v: PRIORITY_LABEL.get(v, v)),
    ("title", "과제명", lambda v: v),
    ("memo", "메모", lambda v: v or "없음"),
]


def _norm(d, key):
    if key == "progress":
        return d.get(key, 0)
    if key == "registered":
        return bool(d.get(key, False))
    return d.get(key, "")


def describe_changes(before, fields):
    parts = []
    for key, label, fmt in TRACKED:
        if key in fields and _norm(before, key) != _norm(fields, key):
            parts.append(f"{label}: {fmt(_norm(before, key))} → {fmt(_norm(fields, key))}")
    return " / ".join(parts)


def stamp(text):
    return {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "text": text}


def demo_items():
    """demo-data.json 의 과제·AI 과제를 오늘 날짜 기준으로 바꿔 돌려준다.

    브라우저 쪽 demo-data.js 의 build() 와 같은 규칙이다 (한쪽을 고치면 다른 쪽도 고칠 것).
    """
    demo = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    now = datetime.now().astimezone()

    def day(n):
        return now - timedelta(days=n)

    def at(n, hour):
        return day(n).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    tasks = []
    for i, d in enumerate(demo["tasks"]):
        n, s, prog = d["daysAgo"], d["s"], d.get("progress", 0)
        hist = [{"at": at(n, 10), "text": "과제 등록"}]
        if s != "todo":
            hist.append({"at": at(int(n * 0.8), 11), "text": "단계: 검토 → 진행"})
        if prog:
            hist.append({"at": at(int(n * 0.5), 14), "text": f"진행률: 0% → {prog}%"})
        if s == "done":
            hist.append({"at": at(int(n * 0.3), 16), "text": "단계: 진행 → 종료"})
        task = {
            "id": int(day(n).replace(hour=10, minute=0, second=0, microsecond=0).timestamp() * 1000) + i,
            "title": d["title"], "s": s, "date": day(n).date().isoformat(),
            "assignee": d.get("assignee", ""), "due": day(-d["dueInDays"]).date().isoformat() if "dueInDays" in d else "",
            "priority": d.get("priority", "mid"), "memo": d.get("memo", ""), "progress": prog,
        }
        if d.get("registered"):
            r = d.get("registeredDaysAgo", 0)
            hist.append({"at": at(r, 17), "text": f"서비스 게시: 해제 → 게시 / 승인자: 없음 → {d['approver']} / 게시 사유: 없음 → {d['regReason']}"})
            task.update(registered=True, approver=d["approver"], regReason=d["regReason"], registeredAt=day(r).date().isoformat())
        task["history"] = hist
        tasks.append(task)
    tasks.sort(key=lambda t: t["id"], reverse=True)   # 최신 등록이 위로

    ai = []
    for i, d in enumerate(demo["aiProjects"]):
        created = at(d["daysAgo"], 10)
        fields = {k: d[k] for k in ("title", "purpose", "category", "stage", "ownerName", "ownerDept", "access")}
        ai.append({"id": int(day(d["daysAgo"]).replace(hour=10, minute=0, second=0, microsecond=0).timestamp() * 1000) + i,
                   **fields, "createdAt": created, "updatedAt": created})
    ai.sort(key=lambda t: t["id"], reverse=True)
    return tasks, ai


class TaskStore:
    """JSON 파일 기반 과제 저장소. 쓰기는 임시 파일 → rename 으로 원자적으로 처리합니다."""

    def __init__(self, path: Path, seed=None):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(seed or [])   # 처음 실행이면 더미데이터로 채움

    def replace_all(self, items):
        with self.lock:
            self._write(items)

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write(self, tasks):
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _new_id(self, tasks):
        used = {t["id"] for t in tasks}
        nid = int(time.time() * 1000)
        while nid in used:
            nid += 1
        return nid

    def list(self):
        with self.lock:
            return self._read()

    def create(self, title, status="todo", created=None, extra=None):
        with self.lock:
            tasks = self._read()
            task = {
                "id": self._new_id(tasks),
                "title": title,
                "s": status,
                "date": created or date.today().isoformat(),
                "assignee": "", "due": "", "priority": "mid", "memo": "",
                "progress": 0, "history": [stamp("과제 등록")],
            }
            task.update(extra or {})
            tasks.insert(0, task)
            self._write(tasks)
            return task

    def update(self, task_id, fields):
        with self.lock:
            tasks = self._read()
            for t in tasks:
                if t["id"] == task_id:
                    text = describe_changes(t, fields)
                    t.update(fields)
                    if text:
                        t["history"] = (t.get("history") or []) + [stamp(text)]
                        t["history"] = t["history"][-MAX_HISTORY:]
                    self._write(tasks)
                    return t
            return None

    def delete(self, task_id):
        with self.lock:
            tasks = self._read()
            kept = [t for t in tasks if t["id"] != task_id]
            if len(kept) == len(tasks):
                return False
            self._write(kept)
            shutil.rmtree(FILES_DIR / str(task_id), ignore_errors=True)
            return True

    def get(self, task_id):
        with self.lock:
            return next((t for t in self._read() if t["id"] == task_id), None)

    def add_file(self, task_id, meta):
        with self.lock:
            tasks = self._read()
            for t in tasks:
                if t["id"] == task_id:
                    t["files"] = (t.get("files") or []) + [meta]
                    t["history"] = ((t.get("history") or []) + [stamp(f"파일 첨부: {meta['name']}")])[-MAX_HISTORY:]
                    self._write(tasks)
                    return meta
            return None

    def remove_file(self, task_id, file_id):
        with self.lock:
            tasks = self._read()
            for t in tasks:
                if t["id"] == task_id:
                    files = t.get("files") or []
                    meta = next((f for f in files if f["id"] == file_id), None)
                    if meta is None:
                        return None
                    t["files"] = [f for f in files if f["id"] != file_id]
                    t["history"] = ((t.get("history") or []) + [stamp(f"파일 삭제: {meta['name']}")])[-MAX_HISTORY:]
                    self._write(tasks)
                    return meta
            return None

    def import_many(self, items):
        with self.lock:
            tasks = self._read()
            added = []
            for it in items:
                used = {t["id"] for t in tasks}
                orig = it.get("id")
                task = {
                    # 원래 id(등록 시각)를 겹치지 않으면 유지 → 대시보드 등록 추이가 원래 날짜로 표시됨
                    "id": orig if isinstance(orig, int) and orig not in used else self._new_id(tasks),
                    "title": it["title"],
                    "s": it["s"],
                    "date": it.get("date") or date.today().isoformat(),
                    "progress": 0,
                    "history": [stamp("브라우저 저장분에서 가져옴")],
                }
                tasks.append(task)
                added.append(task)
            tasks.sort(key=lambda t: t["id"], reverse=True)  # 최신 등록이 위로
            self._write(tasks)
            return added


class AIProjectStore:
    """AI 과제(등록 게시판) 저장소. TaskStore 와 같은 방식(임시 파일 → rename)으로 저장합니다."""

    __init__ = TaskStore.__init__
    replace_all = TaskStore.replace_all
    _read = TaskStore._read
    _write = TaskStore._write
    _new_id = TaskStore._new_id

    def list(self):
        with self.lock:
            return self._read()

    def create(self, fields):
        with self.lock:
            items = self._read()
            check_duplicate(items, fields)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            item = {"id": self._new_id(items), **fields, "createdAt": now, "updatedAt": now}
            items.insert(0, item)
            self._write(items)
            return item

    def update(self, item_id, fields):
        with self.lock:
            items = self._read()
            for it in items:
                if it["id"] == item_id:
                    check_duplicate(items, fields, skip_id=item_id)
                    it.update(fields)
                    it["updatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    self._write(items)
                    return it
            return None

    def delete(self, item_id):
        """지운 과제를 돌려준다 (없으면 None)."""
        with self.lock:
            items = self._read()
            gone = next((it for it in items if it["id"] == item_id), None)
            if gone is not None:
                self._write([it for it in items if it["id"] != item_id])
            return gone

    def create_many(self, rows):
        """여러 건을 한 번에 저장 (모두 저장하거나 하나도 저장하지 않음). 엑셀 순서대로 위에서부터 보이게 한다."""
        with self.lock:
            items = self._read()
            for fields in rows:          # 검사와 저장 사이에 다른 사람이 같은 과제를 등록했을 수도 있어 다시 확인
                check_duplicate(items, fields)
            if len({dup_key(f) for f in rows}) != len(rows):
                raise ValidationError("올리는 파일 안에 명칭·과제책임자가 같은 행이 있습니다.", HTTPStatus.CONFLICT)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            new = []
            for fields in rows:
                item = {"id": self._new_id(items + new), **fields, "createdAt": now, "updatedAt": now}
                new.append(item)
            self._write(new + items)
            return new

    def delete_many(self, ids):
        """지운 과제 목록을 돌려준다."""
        with self.lock:
            items = self._read()
            ids = set(ids)
            gone = [it for it in items if it["id"] in ids]
            if gone:
                self._write([it for it in items if it["id"] not in ids])
            return gone


def dup_key(fields):
    """중복 판단 기준: 명칭 + 과제책임자 (앞뒤·연속 공백과 영문 대소문자는 무시)."""
    norm = lambda v: re.sub(r"\s+", " ", str(v or "")).strip().casefold()
    return norm(fields.get("title")), norm(fields.get("ownerName"))


def check_duplicate(items, fields, skip_id=None):
    key = dup_key(fields)
    if any(it.get("id") != skip_id and dup_key(it) == key for it in items):
        raise ValidationError(f"이미 같은 명칭·과제책임자의 AI 과제가 있습니다. ({fields.get('title')} / {fields.get('ownerName')})", HTTPStatus.CONFLICT)

def clean_title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("과제 이름을 입력하세요.")
    value = value.strip()
    if len(value) > MAX_TITLE:
        raise ValidationError(f"과제 이름은 {MAX_TITLE}자 이하로 입력하세요.")
    return value


def clean_filename(value):
    """경로 문자를 없애고 화면에 보일 파일 이름만 남깁니다. (실제 저장은 무작위 id 로 함)"""
    name = re.split(r"[\\/]", value or "")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if not name or name in (".", ".."):
        raise ValidationError("파일 이름이 올바르지 않습니다.")
    if len(name) > 200:
        raise ValidationError("파일 이름은 200자 이하로 해 주세요.")
    return name


def file_kind(name):
    return FILE_KINDS.get(os.path.splitext(name)[1].lower(), "기타")


def clean_status(value):
    if value not in STATUSES:
        raise ValidationError("상태 값이 올바르지 않습니다.")
    return value


def clean_extra(body):
    """담당자·마감일·우선순위·메모·진행률: 전달된 항목만 검증해 돌려줍니다."""
    out = {}
    if "assignee" in body:
        v = body["assignee"]
        if not isinstance(v, str) or len(v.strip()) > 50:
            raise ValidationError("담당자는 50자 이하로 입력하세요.")
        out["assignee"] = v.strip()
    if "memo" in body:
        v = body["memo"]
        if not isinstance(v, str) or len(v.strip()) > 500:
            raise ValidationError("메모는 500자 이하로 입력하세요.")
        out["memo"] = v.strip()
    if "due" in body:
        v = body["due"]
        if v in ("", None):
            out["due"] = ""
        else:
            try:
                out["due"] = date.fromisoformat(v).isoformat()
            except (TypeError, ValueError):
                raise ValidationError("마감일 형식이 올바르지 않습니다. (YYYY-MM-DD)")
    if "progress" in body:
        v = body["progress"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise ValidationError("진행률은 0~100 사이 정수로 입력하세요.")
        out["progress"] = v
    if "registered" in body:
        v = body["registered"]
        if not isinstance(v, bool):
            raise ValidationError("서비스 게시 값이 올바르지 않습니다.")
        out["registered"] = v
        if v:
            approver = body.get("approver")
            reason = body.get("regReason")
            if not isinstance(approver, str) or not approver.strip():
                raise ValidationError("승인자를 입력하세요.")
            if len(approver.strip()) > 50:
                raise ValidationError("승인자는 50자 이하로 입력하세요.")
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationError("게시 사유를 입력하세요.")
            if len(reason.strip()) > 500:
                raise ValidationError("게시 사유는 500자 이하로 입력하세요.")
            out["approver"] = approver.strip()
            out["regReason"] = reason.strip()
            out["registeredAt"] = date.today().isoformat()
        else:
            out["approver"] = ""
            out["regReason"] = ""
            out["registeredAt"] = ""
    if "priority" in body:
        if body["priority"] not in PRIORITIES:
            raise ValidationError("우선순위 값이 올바르지 않습니다.")
        out["priority"] = body["priority"]
    return out


def clean_ai_project(body):
    """AI 과제 등록·수정 입력을 검증해 저장할 항목만 돌려줍니다 (모든 항목 필수)."""
    def text(key, label, limit):
        v = body.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValidationError(f"{label}을(를) 입력하세요.")
        if len(v.strip()) > limit:
            raise ValidationError(f"{label}은(는) {limit}자 이하로 입력하세요.")
        return v.strip()

    def choice(key, label, allowed):
        v = body.get(key)
        if v not in allowed:
            raise ValidationError(f"{label} 값이 올바르지 않습니다.")
        return v

    return {
        "title": text("title", "명칭", MAX_TITLE),
        "purpose": text("purpose", "목적", 2000),
        "category": choice("category", "분류", AI_CATEGORIES),
        "stage": choice("stage", "단계", AI_STAGES),
        "ownerName": text("ownerName", "과제책임자 이름", 50),
        "ownerDept": text("ownerDept", "과제책임자 소속부서", 100),
        "access": choice("access", "접근권한범위", AI_ACCESS),
    }


_demo_tasks, _demo_ai = demo_items()
store = TaskStore(DATA_FILE, seed=_demo_tasks)
ai_store = AIProjectStore(AI_DATA_FILE, seed=_demo_ai)


def reset_demo():
    """과제·AI 과제·사용자를 더미데이터로 되돌린다 (첨부 파일은 지움)."""
    tasks, ai = demo_items()
    store.replace_all(tasks)
    ai_store.replace_all(ai)
    users_store.reset_demo()
    shutil.rmtree(FILES_DIR, ignore_errors=True)


class Handler(AuthMixin, SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    # ---- 공통 ----
    def _json(self, status, payload=None, cookie=None):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValidationError("요청이 너무 큽니다.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValidationError("JSON 형식이 올바르지 않습니다.")

    def _task_id(self):
        try:
            return int(self.path.split("?")[0].rstrip("/").rsplit("/", 1)[1])
        except ValueError:
            return None

    def _route(self):
        return self.path.split("?")[0].rstrip("/")

    def _handle(self, fn):
        try:
            fn()
        except ValidationError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:  # 서버 오류도 JSON 으로 돌려줌
            self.log_error("error: %r", e)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "저장 중 서버 오류가 발생했습니다."})

    # ---- 첨부 파일 ----
    def _file_list(self, task_id):
        task = store.get(task_id)
        if task is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "과제를 찾을 수 없습니다."})
        self._json(HTTPStatus.OK, task.get("files") or [])

    def _file_upload(self, task_id):
        if store.get(task_id) is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "과제를 찾을 수 없습니다."})
        query = parse_qs(urlsplit(self.path).query)
        name = clean_filename((query.get("name") or [""])[0])
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            raise ValidationError("파일 크기를 알 수 없습니다.")
        if length > MAX_FILE:
            raise ValidationError(f"파일은 {MAX_FILE // (1024 * 1024)}MB 이하만 올릴 수 있습니다.")
        file_id = uuid.uuid4().hex
        folder = FILES_DIR / str(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / file_id
        remaining = length
        with path.open("wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            path.unlink(missing_ok=True)
            raise ValidationError("파일을 끝까지 받지 못했습니다. 다시 올려 주세요.")
        meta = {"id": file_id, "name": name, "size": length, "kind": file_kind(name),
                "uploadedAt": datetime.now().isoformat(timespec="seconds")}
        if store.add_file(task_id, meta) is None:
            path.unlink(missing_ok=True)
            return self._json(HTTPStatus.NOT_FOUND, {"error": "과제를 찾을 수 없습니다."})
        self._json(HTTPStatus.CREATED, meta)

    def _file_download(self, task_id, file_id):
        task = store.get(task_id)
        meta = next((f for f in (task or {}).get("files") or [] if f["id"] == file_id), None)
        path = FILES_DIR / str(task_id) / file_id
        if meta is None or not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "파일을 찾을 수 없습니다."})
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        # 올린 html 이 이 사이트 안에서 실행되지 않도록 항상 '내려받기'로만 보낸다
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(meta['name'])}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _file_delete(self, task_id, file_id):
        if store.remove_file(task_id, file_id) is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "파일을 찾을 수 없습니다."})
        (FILES_DIR / str(task_id) / file_id).unlink(missing_ok=True)
        self._json(HTTPStatus.NO_CONTENT)

    # ---- 로그인·권한 검사를 거쳐 실행 ----
    def _handle_read(self, fn):
        """조회: 로그인한 사용자면 누구나."""
        def run():
            self._require()
            fn()
        self._handle(run)

    def _handle_write(self, fn):
        """등록·수정·삭제: 사이트관리자·과제책임자만 (Origin 검사 포함)."""
        def run():
            self._require_writer()
            fn()
        self._handle(run)

    # ---- AI 과제 엑셀 (사이트관리자) ----
    def _require_admin_write(self):
        self._check_origin()
        return self._require(admin=True)

    def _send_xlsx(self, data, filename):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _ai_xlsx(self, rows, extra_headers=()):
        heads = [c[1] for c in AI_COLUMNS] + list(extra_headers)
        widths = [c[3] for c in AI_COLUMNS] + [20] * len(extra_headers)
        idx = {k: i for i, (k, *_r) in enumerate(AI_COLUMNS)}
        choices = {idx["category"]: AI_CATEGORIES, idx["stage"]: AI_STAGES, idx["access"]: AI_ACCESS}
        return xlsx_util.write_xlsx(heads, rows, sheet="AI 과제", widths=widths, choices=choices)

    def _ai_export(self):
        self._require(admin=True)
        fmt = lambda v: str(v or "").replace("T", " ")[:19]
        items = sorted(ai_store.list(), key=lambda t: str(t.get("updatedAt", "")), reverse=True)
        rows = [[t.get(k, "") for k, *_r in AI_COLUMNS] + [fmt(t.get("createdAt")), fmt(t.get("updatedAt"))] for t in items]
        self._send_xlsx(self._ai_xlsx(rows, ("등록일시", "수정일시")), f"AI과제목록_{date.today().strftime('%Y%m%d')}.xlsx")

    def _ai_template(self):
        self._require(admin=True)
        sample = [["(예시) 회의록 요약 도우미", "회의 녹취를 요약해 결정 사항을 정리한다. 이 줄은 지우고 쓰세요.", "개발", "기획중", "홍길동", "데이터운영팀", "소속부서"]]
        self._send_xlsx(self._ai_xlsx(sample), "AI과제_올리기_양식.xlsx")

    def _ai_import(self):
        self._require_admin_write()
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length <= 0:
            raise ValidationError("엑셀 파일을 선택하세요.")
        if length > MAX_XLSX:
            raise ValidationError(f"엑셀 파일은 {MAX_XLSX // (1024 * 1024)}MB 이하만 올릴 수 있습니다.")
        data = self.rfile.read(length)
        try:
            table = xlsx_util.read_xlsx(data, max_rows=MAX_IMPORT_ROWS)
        except xlsx_util.XlsxError as e:
            raise ValidationError(str(e))
        # 머리글 줄: 내용이 있는 첫 줄 (read_xlsx 는 내용이 있는 줄만 행 번호와 함께 돌려준다)
        if not table:
            raise ValidationError("엑셀 파일이 비어 있습니다.")
        norm = lambda v: re.sub(r"[\s*]", "", str(v))
        heads = [norm(v) for v in table[0][1]]
        col = {}
        for key, label, alias, _w in AI_COLUMNS:
            for name in (label, *alias):
                if norm(name) in heads:
                    col[key] = heads.index(norm(name))
                    break
        missing = [label for key, label, *_r in AI_COLUMNS if key not in col]
        if missing:
            raise ValidationError("첫 줄(머리글)에 이 열이 없습니다: " + ", ".join(missing) + ". '양식 내려받기'로 받은 파일에 맞춰 주세요.")
        ok_rows, errors = [], []
        existing = {dup_key(it): it for it in ai_store.list()}
        seen = {}                             # 파일 안에서 먼저 나온 같은 과제의 행 번호
        for n, r in table[1:]:
            cell = lambda k: str(r[col[k]]).strip() if col[k] < len(r) else ""
            body = {k: cell(k) for k in col}
            if not any(body.values()):
                continue                      # 빈 줄은 건너뜀
            if body["title"].startswith("(예시)"):
                continue                      # 양식의 예시 줄
            try:
                fields = clean_ai_project(body)
            except ValidationError as e:
                errors.append({"row": n, "error": str(e)})
                continue
            key = dup_key(fields)
            if key in existing:
                errors.append({"row": n, "error": "이미 등록된 AI 과제와 명칭·과제책임자가 같습니다 (중복)."})
            elif key in seen:
                errors.append({"row": n, "error": f"{seen[key]}행과 명칭·과제책임자가 같습니다 (중복)."})
            else:
                seen[key] = n
                ok_rows.append({"row": n, **fields})
        if not ok_rows and not errors:
            raise ValidationError("등록할 AI 과제가 없습니다. 둘째 줄부터 내용을 넣어 주세요.")
        if len(ok_rows) + len(errors) > MAX_IMPORT_ROWS:
            raise ValidationError(f"한 번에 {MAX_IMPORT_ROWS}건까지만 올릴 수 있습니다.")
        dry = parse_qs(urlsplit(self.path).query).get("dry") == ["1"]
        if dry or errors:
            status = HTTPStatus.OK if dry else HTTPStatus.BAD_REQUEST
            return self._json(status, {"dry": True, "count": len(ok_rows), "rows": ok_rows[:200], "errors": errors,
                                       **({} if dry else {"error": f"틀린 행이 {len(errors)}개 있어 아무것도 저장하지 않았습니다."})})
        created = ai_store.create_many([{k: v for k, v in row.items() if k != "row"} for row in ok_rows])
        name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
        work_log.record(self._user(), "ai_import", created, file_name=name)
        self._json(HTTPStatus.CREATED, {"created": len(created)})

    def _ai_bulk_delete(self):
        self._require_admin_write()
        body = self._body()
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids or len(ids) > 5000:
            raise ValidationError("삭제할 AI 과제를 선택하세요.")
        clean = []
        for v in ids:
            try:
                clean.append(int(v))
            except (TypeError, ValueError):
                raise ValidationError("AI 과제 ID가 올바르지 않습니다.")
        gone = ai_store.delete_many(clean)
        if gone:
            work_log.record(self._user(), "ai_bulk_delete", gone)
        self._json(HTTPStatus.OK, {"deleted": len(gone)})

    def _work_log(self):
        self._require(admin=True)
        self._json(HTTPStatus.OK, {"actions": work_log.ACTIONS, "items": work_log.read()})

    # ---- 정적 파일: data/ 폴더·서버 코드·숨김 파일은 직접 노출하지 않음 ----
    def do_GET(self):
        route = self._route()
        if self.auth_get(route) or similar_llm.handle(self, "GET", route):
            return
        m = FILE_ROUTE.match(route)
        if m:
            tid, fid = int(m.group(1)), m.group(2)
            return self._handle_read(lambda: self._file_download(tid, fid) if fid else self._file_list(tid))
        if route == "/api/tasks":
            return self._handle_read(lambda: self._json(HTTPStatus.OK, store.list()))
        if route == "/api/ai-projects":
            return self._handle_read(lambda: self._json(HTTPStatus.OK, ai_store.list()))
        if route == "/api/ai-projects/export":
            return self._handle(self._ai_export)
        if route == "/api/ai-projects/template":
            return self._handle(self._ai_template)
        if route == "/api/work-log":
            return self._handle(self._work_log)
        if (route.startswith("/api/") or route.startswith("/data") or route.endswith((".py", ".pyc"))
                or "/." in route or "__pycache__" in route):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        route = self._route()

        def create():
            body = self._body()
            task = store.create(clean_title(body.get("title")), extra=clean_extra(body))
            self._json(HTTPStatus.CREATED, task)

        def import_many():
            body = self._body()
            items = body.get("tasks")
            if not isinstance(items, list):
                raise ValidationError("tasks 배열이 필요합니다.")
            cleaned = [
                {
                    "title": clean_title(it.get("title")),
                    "s": it.get("s") if it.get("s") in STATUSES else "todo",
                    "date": it.get("date") if isinstance(it.get("date"), str) else None,
                    "id": it.get("id") if isinstance(it.get("id"), int) and not isinstance(it.get("id"), bool) else None,
                }
                for it in items
                if isinstance(it, dict)
            ]
            self._json(HTTPStatus.CREATED, store.import_many(cleaned))

        if self.auth_post(route) or similar_llm.handle(self, "POST", route):
            return
        m = FILE_ROUTE.match(route)
        if m and not m.group(2):
            return self._handle_write(lambda: self._file_upload(int(m.group(1))))
        if route == "/api/tasks":
            return self._handle_write(create)
        if route == "/api/ai-projects":
            return self._handle_write(lambda: self._json(HTTPStatus.CREATED, ai_store.create(clean_ai_project(self._body()))))
        if route == "/api/tasks/import":
            return self._handle_write(import_many)
        if route == "/api/ai-projects/import":
            return self._handle(self._ai_import)
        if route == "/api/ai-projects/bulk-delete":
            return self._handle(self._ai_bulk_delete)
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self):
        if similar_llm.handle(self, "PUT", self._route()):
            return
        if not self._route().startswith("/api/ai-projects/"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def update():
            item = ai_store.update(self._task_id(), clean_ai_project(self._body()))
            if item is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "AI 과제를 찾을 수 없습니다."})
            self._json(HTTPStatus.OK, item)

        self._handle_write(update)

    def do_PATCH(self):
        if self.auth_patch(self._route()):
            return
        if not self._route().startswith("/api/tasks/"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def update():
            task_id = self._task_id()
            body = self._body()
            fields = {}
            if "title" in body:
                fields["title"] = clean_title(body["title"])
            if "s" in body:
                fields["s"] = clean_status(body["s"])
            fields.update(clean_extra(body))
            if "registered" in fields and self._user()["role"] != "admin":
                raise ValidationError("서비스 게시·해제는 사이트관리자만 할 수 있습니다.", HTTPStatus.FORBIDDEN)
            if fields.get("registered"):
                cur = next((t for t in store.list() if t["id"] == task_id), None)
                if cur is not None and fields.get("progress", cur.get("progress", 0)) != 100:
                    raise ValidationError("진행률 100%인 과제만 서비스 게시할 수 있습니다.")
            if not fields:
                raise ValidationError("수정할 항목이 없습니다.")
            task = store.update(task_id, fields)
            if task is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "과제를 찾을 수 없습니다."})
            self._json(HTTPStatus.OK, task)

        self._handle_write(update)

    def do_DELETE(self):
        route = self._route()
        if similar_llm.handle(self, "DELETE", route):
            return
        m = FILE_ROUTE.match(route)
        if m:
            if not m.group(2):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._handle_write(lambda: self._file_delete(int(m.group(1)), m.group(2)))
        if not (route.startswith("/api/tasks/") or route.startswith("/api/ai-projects/")):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        target = ai_store if route.startswith("/api/ai-projects/") else store

        def delete():
            gone = target.delete(self._task_id())
            if gone:
                if target is ai_store:
                    work_log.record(self._user(), "ai_delete", [gone])
                self._json(HTTPStatus.NO_CONTENT)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "과제를 찾을 수 없습니다."})

        self._handle_write(delete)


def main():
    parser = argparse.ArgumentParser(description="과제 관리 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8885)
    parser.add_argument("--reset-demo", action="store_true", help="과제·AI 과제·사용자를 demo-data.json 더미데이터로 되돌린 뒤 시작")
    args = parser.parse_args()
    if args.reset_demo:
        reset_demo()
        print("더미데이터로 되돌렸습니다: 과제·AI 과제·사용자 (첨부 파일 삭제)")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"과제 관리 서버: http://{args.host}:{args.port}  (저장 파일: {DATA_FILE})")
    print(f"시연 계정: 100001(사이트관리자) / 100002(과제책임자) / 100003(과제참여자) — 비밀번호 {DEFAULT_SEED_PASSWORD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")


if __name__ == "__main__":
    main()
