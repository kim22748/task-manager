"""공유 게시판 — 서비스 게시 완료된 과제를 과제책임자가 설명·파일과 함께 소개하는 게시판.

세부 진행 현황·과제 상세와는 따로 움직인다. 글을 쓸 때 '서비스 게시 완료된 내 과제'인지만 확인하고,
글에는 과제명만 복사해 둔다(과제가 나중에 바뀌거나 지워져도 글은 그대로 남는다).

    GET    /api/board                       (로그인 사용자) 게시글 목록 (최신순)
    POST   /api/board                       (과제책임자·사이트관리자) 글쓰기 {"taskId", "title", "body"}
                                            — 서비스 게시 완료된 과제만, 과제책임자는 자기 과제만
    PATCH  /api/board/<id>                  (글쓴이·사이트관리자) 수정 {"title", "body"}
    DELETE /api/board/<id>                  (글쓴이·사이트관리자) 삭제 (첨부 파일도 함께)
    POST   /api/board/<id>/files?name=..    (글쓴이·사이트관리자) 첨부 올리기 (요청 본문 = 파일 내용, 20MB 이하, 글 하나에 10개까지)
    GET    /api/board/<id>/files/<fileId>   (로그인 사용자) 첨부 내려받기
    DELETE /api/board/<id>/files/<fileId>   (글쓴이·사이트관리자) 첨부 삭제

저장: data/board.json, 첨부는 data/board_files/<글 id>/<파일 id>
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import db
from auth_users import ValidationError

BASE_DIR = Path(__file__).resolve().parent
BOARD_FILE = BASE_DIR / "data" / "board.json"
BOARD_FILES_DIR = BASE_DIR / "data" / "board_files"
MAX_FILE = 20 * 1024 * 1024
MAX_FILES_PER_POST = 10
MAX_TITLE = 200
MAX_BODY = 3000
ROUTE = re.compile(r"^/api/board(?:/(\d+)(?:/files(?:/([0-9a-f]{32}))?)?)?$")

_lock = db.tx      # `with _lock:` 한 묶음이 DB 트랜잭션 하나 (SQLite: board_posts 표, db.py)


def _read():
    with db.read():
        return db.BOARD_POSTS.load()


def _write(posts):
    with db.tx:
        db.BOARD_POSTS.save(posts)


# 예전 JSON 파일(data/board.json)이 남아 있으면 처음 한 번 DB 로 옮긴다
db.adopt("board_posts", BOARD_FILE, _write)


def reset():
    """시연 데이터로 되돌릴 때 게시글과 첨부도 비운다."""
    with _lock:
        _write([])
    shutil.rmtree(BOARD_FILES_DIR, ignore_errors=True)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _text(value, label, limit, multiline=False):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}을(를) 입력하세요.")
    keep = (9, 10) if multiline else ()
    value = "".join(ch for ch in value.replace("\r\n", "\n") if (ord(ch) >= 32 and ord(ch) != 127) or ord(ch) in keep).strip()
    if not value:
        raise ValidationError(f"{label}을(를) 입력하세요.")
    if len(value) > limit:
        raise ValidationError(f"{label}은(는) {limit}자 이하로 입력하세요.")
    return value


def _filename(value):
    name = re.split(r"[\\/]", value or "")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if not name or name in (".", "..") or len(name) > 200:
        raise ValidationError("파일 이름이 올바르지 않습니다.")
    return name


def _can_edit(user, post):
    return user["role"] == "admin" or post.get("authorEmpNo") == user.get("empNo")


def _find(posts, post_id):
    return next((p for p in posts if p["id"] == post_id), None)


def _owned(h, post_id):
    """수정·삭제·첨부: 글쓴이와 사이트관리자만. (posts, post) 를 돌려준다. _lock 을 잡은 채로 부른다."""
    user = h._require_writer()
    posts = _read()
    post = _find(posts, post_id)
    if post is None:
        raise ValidationError("게시글을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
    if not _can_edit(user, post):
        raise ValidationError("글쓴이와 사이트관리자만 할 수 있습니다.", HTTPStatus.FORBIDDEN)
    return posts, post


def _create(h, store):
    user = h._require_writer()
    body = h._body()
    if not isinstance(body, dict):
        raise ValidationError("JSON 형식이 올바르지 않습니다.")
    task = next((t for t in store.list() if t["id"] == body.get("taskId")), None)
    if task is None:
        raise ValidationError("과제를 선택하세요.")
    if not task.get("registered"):
        raise ValidationError("서비스 게시 완료된 과제만 올릴 수 있습니다.")
    if user["role"] != "admin" and (task.get("assignee") or "").strip() != (user.get("name") or "").strip():
        raise ValidationError("내가 과제책임자인 과제만 올릴 수 있습니다.", HTTPStatus.FORBIDDEN)
    now = _now()
    with _lock:
        posts = _read()
        used = {p["id"] for p in posts}
        pid = int(time.time() * 1000)
        while pid in used:
            pid += 1
        post = {"id": pid, "taskId": task["id"], "taskTitle": task.get("title", ""),
                "title": _text(body.get("title"), "제목", MAX_TITLE), "body": _text(body.get("body"), "설명", MAX_BODY, multiline=True),
                "authorEmpNo": user.get("empNo", ""), "authorName": user.get("name", ""), "authorDept": user.get("dept", ""),
                "createdAt": now, "updatedAt": now, "files": []}
        posts.insert(0, post)
        _write(posts)
    h._json(HTTPStatus.CREATED, post)


def _update(h, post_id):
    body = h._body()
    if not isinstance(body, dict):
        raise ValidationError("JSON 형식이 올바르지 않습니다.")
    with _lock:
        posts, post = _owned(h, post_id)
        if "title" in body:
            post["title"] = _text(body["title"], "제목", MAX_TITLE)
        if "body" in body:
            post["body"] = _text(body["body"], "설명", MAX_BODY, multiline=True)
        post["updatedAt"] = _now()
        _write(posts)
    h._json(HTTPStatus.OK, post)


def _delete(h, post_id):
    with _lock:
        posts, post = _owned(h, post_id)
        posts.remove(post)
        _write(posts)
    shutil.rmtree(BOARD_FILES_DIR / str(post_id), ignore_errors=True)
    h._json(HTTPStatus.NO_CONTENT)


def _upload(h, post_id):
    with _lock:
        _posts, post = _owned(h, post_id)
        if len(post.get("files") or []) >= MAX_FILES_PER_POST:
            raise ValidationError(f"첨부 파일은 글 하나에 {MAX_FILES_PER_POST}개까지 올릴 수 있습니다.")
    name = _filename((parse_qs(urlsplit(h.path).query).get("name") or [""])[0])
    try:
        length = int(h.headers.get("Content-Length") or -1)
    except ValueError:
        length = -1
    if length < 0:
        raise ValidationError("파일 크기를 알 수 없습니다.")
    if length > MAX_FILE:
        raise ValidationError(f"파일은 {MAX_FILE // (1024 * 1024)}MB 이하만 올릴 수 있습니다.")
    file_id = uuid.uuid4().hex
    folder = BOARD_FILES_DIR / str(post_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_id
    remaining = length
    with path.open("wb") as f:
        while remaining > 0:
            chunk = h.rfile.read(min(65536, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    if remaining:
        path.unlink(missing_ok=True)
        raise ValidationError("파일을 끝까지 받지 못했습니다. 다시 올려 주세요.")
    meta = {"id": file_id, "name": name, "size": length, "uploadedAt": _now()}
    with _lock:
        posts = _read()
        post = _find(posts, post_id)
        if post is None or len(post.get("files") or []) >= MAX_FILES_PER_POST:
            path.unlink(missing_ok=True)
            raise ValidationError("게시글을 찾을 수 없거나 첨부 파일이 너무 많습니다.", HTTPStatus.NOT_FOUND)
        post["files"] = (post.get("files") or []) + [meta]
        _write(posts)
    h._json(HTTPStatus.CREATED, meta)


def _download(h, post_id, file_id):
    h._require()
    with _lock:
        post = _find(_read(), post_id)
    meta = next((f for f in (post or {}).get("files") or [] if f["id"] == file_id), None)
    path = BOARD_FILES_DIR / str(post_id) / file_id
    if meta is None or not path.is_file():
        return h._json(HTTPStatus.NOT_FOUND, {"error": "파일을 찾을 수 없습니다."})
    h.send_response(HTTPStatus.OK)
    # 올린 html 이 이 사이트 안에서 실행되지 않도록 항상 '내려받기'로만 보낸다
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(meta['name'])}")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Length", str(path.stat().st_size))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    with path.open("rb") as f:
        shutil.copyfileobj(f, h.wfile)


def _file_delete(h, post_id, file_id):
    with _lock:
        posts, post = _owned(h, post_id)
        files = post.get("files") or []
        if not any(f["id"] == file_id for f in files):
            raise ValidationError("파일을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        post["files"] = [f for f in files if f["id"] != file_id]
        _write(posts)
    (BOARD_FILES_DIR / str(post_id) / file_id).unlink(missing_ok=True)
    h._json(HTTPStatus.NO_CONTENT)


def handle(h, method, route, store):
    """/api/board* 요청을 처리한다. 처리했으면 True, 아니면 False. h 는 server.py 의 Handler, store 는 과제 저장소."""
    m = ROUTE.match(route)
    if not m:
        return False
    pid = int(m.group(1)) if m.group(1) else None
    is_files = "/files" in route
    fid = m.group(2)

    def listing():
        h._require()
        with _lock:
            posts = _read()
        h._json(HTTPStatus.OK, sorted(posts, key=lambda p: str(p.get("createdAt", "")), reverse=True))

    table = {
        ("GET", False, False, False): listing,
        ("POST", False, False, False): lambda: _create(h, store),
        ("PATCH", True, False, False): lambda: _update(h, pid),
        ("DELETE", True, False, False): lambda: _delete(h, pid),
        ("POST", True, True, False): lambda: _upload(h, pid),
        ("GET", True, True, True): lambda: _download(h, pid, fid),
        ("DELETE", True, True, True): lambda: _file_delete(h, pid, fid),
    }
    fn = table.get((method, pid is not None, is_files, fid is not None))
    if fn is None:
        h._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
    else:
        h._handle(fn)
    return True
