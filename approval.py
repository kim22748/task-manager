"""신규 과제 등록 신청 · 승인 — 일반회원이 '신규작성'을 하면 바로 등록되지 않고 신청으로 쌓이고,
사이트관리자가 승인해야 과제(AI 과제 + 과제 진행)로 등록된다. 사이트관리자가 직접 만든 과제는 바로 등록된다.

    POST   /api/ai-projects                        일반회원이 보내면 server.py 가 submit() 으로 넘긴다 → 202 {"pending": true, ...}
    GET    /api/project-requests                   (로그인 사용자) 사이트관리자는 전체, 일반회원은 자기 신청만
    POST   /api/project-requests/<id>/approve      (사이트관리자) 승인 → 과제로 등록하고 신청은 지운다
    POST   /api/project-requests/<id>/reject       (사이트관리자) 반려 {"reason"} → 신청자에게 사유가 보인다
    DELETE /api/project-requests/<id>              (신청자·사이트관리자) 신청 취소 / 반려된 신청 지우기

저장: data/project_requests.json  [{id, fields{명칭·목적·분류·단계·과제책임자·소속부서·공개 여부}, requester*, requestedAt, status(pending|rejected), reason, decidedAt, decidedBy}]
"""

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import work_log
from auth_users import ValidationError

REQ_FILE = Path(__file__).resolve().parent / "data" / "project_requests.json"
ROUTE = re.compile(r"^/api/project-requests(?:/(\d+)(?:/(approve|reject))?)?$")
MAX_PENDING_PER_USER = 20
MAX_REASON = 300

_lock = threading.Lock()


def _read():
    try:
        with REQ_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(items):
    REQ_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=REQ_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REQ_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def reset():
    """시연 데이터로 되돌릴 때 등록 신청도 비운다."""
    with _lock:
        _write([])


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _key(fields):
    """중복 판단 기준: 명칭 + 과제책임자 (server.py 의 dup_key 와 같은 규칙)."""
    norm = lambda v: re.sub(r"\s+", " ", str(v or "")).strip().casefold()
    return norm(fields.get("title")), norm(fields.get("ownerName"))


def submit(h, user, fields, ai_store):
    """일반회원의 신규작성을 승인 대기 신청으로 저장한다. fields 는 server.py 가 검증을 마친 값."""
    key = _key(fields)
    if any(_key(it) == key for it in ai_store.list()):
        raise ValidationError(f"이미 같은 명칭·과제책임자의 과제가 있습니다. ({fields.get('title')} / {fields.get('ownerName')})", HTTPStatus.CONFLICT)
    with _lock:
        items = _read()
        mine = [r for r in items if r.get("requesterEmpNo") == user.get("empNo") and r.get("status") == "pending"]
        if any(_key(r.get("fields") or {}) == key for r in mine):
            raise ValidationError("같은 명칭으로 이미 등록을 신청해 승인을 기다리고 있습니다.", HTTPStatus.CONFLICT)
        if len(mine) >= MAX_PENDING_PER_USER:
            raise ValidationError(f"승인 대기 중인 신청이 너무 많습니다. (최대 {MAX_PENDING_PER_USER}건)")
        used = {r["id"] for r in items}
        rid = int(time.time() * 1000)
        while rid in used:
            rid += 1
        req = {"id": rid, "fields": fields, "requesterEmpNo": user.get("empNo", ""), "requesterName": user.get("name", ""),
               "requesterDept": user.get("dept", ""), "requestedAt": _now(), "status": "pending"}
        items.insert(0, req)
        _write(items)
    h._json(HTTPStatus.ACCEPTED, {"pending": True, **req})


def _admin(h):
    h._check_origin()
    return h._require(admin=True)


def _approve(h, rid, ai_store, sync):
    admin = _admin(h)
    with _lock:
        items = _read()
        req = next((r for r in items if r["id"] == rid), None)
        if req is None:
            raise ValidationError("등록 신청을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        item = ai_store.create(dict(req["fields"]))   # 같은 명칭·과제책임자가 그사이 생겼으면 여기서 409
        items.remove(req)
        _write(items)
    sync()   # 과제 진행 기록을 함께 만든다
    work_log.record(admin, "request_approve", [item], detail=f"신청자 {req.get('requesterName', '')}({req.get('requesterEmpNo', '')})의 신규 과제 등록 승인")
    h._json(HTTPStatus.CREATED, item)


def _reject(h, rid):
    admin = _admin(h)
    body = h._body()
    reason = body.get("reason") if isinstance(body, dict) else ""
    reason = "".join(ch for ch in str(reason or "") if ord(ch) >= 32 and ord(ch) != 127).strip()
    if not reason:
        raise ValidationError("반려 사유를 입력하세요.")
    if len(reason) > MAX_REASON:
        raise ValidationError(f"반려 사유는 {MAX_REASON}자 이하로 입력하세요.")
    with _lock:
        items = _read()
        req = next((r for r in items if r["id"] == rid), None)
        if req is None:
            raise ValidationError("등록 신청을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        if req.get("status") != "pending":
            raise ValidationError("이미 반려된 신청입니다.")
        req.update(status="rejected", reason=reason, decidedAt=_now(), decidedBy=admin.get("name", ""))
        _write(items)
    work_log.record(admin, "request_reject", [{**req["fields"], "id": rid}], detail=f"신규 과제 등록 반려: {reason}")
    h._json(HTTPStatus.OK, req)


def _delete(h, rid):
    h._check_origin()
    user = h._require()
    with _lock:
        items = _read()
        req = next((r for r in items if r["id"] == rid), None)
        if req is None:
            raise ValidationError("등록 신청을 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        if user["role"] != "admin" and req.get("requesterEmpNo") != user.get("empNo"):
            raise ValidationError("신청자와 사이트관리자만 할 수 있습니다.", HTTPStatus.FORBIDDEN)
        items.remove(req)
        _write(items)
    h._json(HTTPStatus.NO_CONTENT)


def _list(h):
    user = h._require()
    with _lock:
        items = _read()
    if user["role"] != "admin":
        items = [r for r in items if r.get("requesterEmpNo") == user.get("empNo")]
    h._json(HTTPStatus.OK, items)


def handle(h, method, route, ai_store, sync):
    """/api/project-requests* 요청을 처리한다. 처리했으면 True, 아니면 False."""
    m = ROUTE.match(route)
    if not m:
        return False
    rid = int(m.group(1)) if m.group(1) else None
    action = m.group(2)
    if method == "GET" and rid is None:
        fn = lambda: _list(h)
    elif method == "POST" and action == "approve":
        fn = lambda: _approve(h, rid, ai_store, sync)
    elif method == "POST" and action == "reject":
        fn = lambda: _reject(h, rid)
    elif method == "DELETE" and rid is not None and action is None:
        fn = lambda: _delete(h, rid)
    else:
        h._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    h._handle(fn)
    return True
