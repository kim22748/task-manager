"""A01 사용자 관리 — 로그인·회원가입·가입 승인·역할 관리 (msshinlh/server.py 에서 옮겨 옴).

server.py 가 이 모듈을 불러 씁니다. Python 3 표준 라이브러리만 사용합니다.

계정 구분(role):  admin = 사이트관리자 / user = 일반회원  (가입자는 사이트관리자를 빼고 모두 일반회원)
과제책임자·과제참여자는 계정이 아니라 과제별 역할입니다: 과제를 등록한 사람이 그 과제의 과제책임자, 과제책임자가 초대한 사람이 과제참여자.
상태(status): pending(승인 대기) / active(사용 중) / inactive(비활성) / rejected(반려)

보안: 비밀번호 PBKDF2 해시 · HttpOnly 세션 · (IP,사번)별 5회 실패 잠금 · Origin 검사(CSRF) · 관리자 작업 기록(data/audit.log)

API:
    POST   /api/auth/signup     {"empNo"(6자리 숫자),"name","dept","email"(@lh.or.kr),"password"}  → 승인 대기 계정 생성
    POST   /api/auth/login      {"empNo","password"}                         → 세션 쿠키 발급
    POST   /api/auth/logout
    GET    /api/auth/me                                                      → 현재 로그인 사용자
    POST   /api/auth/password   {"current","new"}                            → 내 비밀번호 변경
    GET    /api/users           (admin) 전체 사용자 목록
    PATCH  /api/users/<id>      (admin) {"action": "approve|reject|deactivate|activate|resetPassword"}
    GET    /api/audit           (admin) 최근 처리 이력 200건
    GET    /api/users/directory (로그인 사용자) 사용 중인 사용자의 id·이름·부서·역할
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "data" / "users.json"
AUDIT_FILE = BASE_DIR / "data" / "audit.log"
DEMO_FILE = BASE_DIR / "demo-data.json"
ROLES = ["admin", "user"]   # 사이트관리자 / 일반회원. 예전 데이터의 owner·member 는 읽을 때 user 로 바꾼다


def account_role(role):
    return "admin" if role == "admin" else "user"
PBKDF2_ITER = 200_000
SESSION_TTL = 8 * 3600
MAX_FAILS, LOCK_SECONDS = 5, 300
COOKIE = "lh_session"
FAIL_MAX_KEYS = 5000   # 로그인 실패 기록 최대 개수 (메모리 보호)
EMAIL_DOMAIN = "@lh.or.kr"
DEFAULT_SEED_PASSWORD = "Lh!2026pw"  # 더미 계정 공통 비밀번호 (시연 전용 — 운영 전 삭제)


class ValidationError(Exception):
    """사용자에게 보여 줄 오류. status 로 HTTP 응답 코드를 정한다 (기본 400)."""

    def __init__(self, msg, status=HTTPStatus.BAD_REQUEST):
        super().__init__(msg)
        self.status = status


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITER).hex()
    return salt, h


def verify_password(password, salt, expected):
    return hmac.compare_digest(hash_password(password, salt)[1], expected)


def audit(actor, action, target="", detail=""):
    """누가 언제 무엇을 했는지 DB(audit_log 표)에 남긴다. 비밀번호 등은 기록하지 않는다."""
    db.audit_add({"at": now_iso(), "actor": actor, "action": action, "target": target, "detail": detail})


def read_audit(limit=200):
    """최근 기록부터 limit 건."""
    return db.audit_read(limit)


# 예전 기록 파일(data/audit.log)이 남아 있으면 처음 한 번 DB 로 옮긴다
db.adopt("audit_log", AUDIT_FILE, lambda recs: [db.audit_add(r) for r in recs if isinstance(r, dict)], load=db.legacy_lines)


def make_seed_users():
    """demo-data.json 의 더미 사용자로 계정을 만든다. 비밀번호는 모두 DEFAULT_SEED_PASSWORD."""
    demo = json.loads(DEMO_FILE.read_text(encoding="utf-8"))["users"]
    salt, h = hash_password(DEFAULT_SEED_PASSWORD)
    now = datetime.now().astimezone()
    users = []
    for i, d in enumerate(demo, 1):
        created = (now - timedelta(days=d.get("daysAgo", 0))).isoformat(timespec="seconds")
        users.append({
            "id": i, "empNo": d["empNo"], "name": d["name"], "dept": d["dept"],
            "email": f"user{d['empNo']}{EMAIL_DOMAIN}", "role": account_role(d["role"]), "status": d["status"],
            "salt": salt, "pwHash": h, "createdAt": created,
            "approvedAt": created if d["status"] in ("active", "inactive") else "", "lastLoginAt": "",
        })
    return users


class UserStore:
    """사용자 저장소 (SQLite: users 표, db.py). `with self.lock:` 한 묶음이 DB 트랜잭션 하나입니다."""

    def __init__(self, path):
        self.path = path                # 예전 JSON 파일. 남아 있으면 처음 한 번 DB 로 옮긴다
        self.lock = db.tx
        db.adopt("users", path, self._write, default=make_seed_users)

    def reset_demo(self):
        with self.lock:
            self._write(make_seed_users())

    def _read(self):
        with db.read():
            return db.USERS.load()

    def _write(self, users):
        users = [dict(u) for u in users if isinstance(u, dict)]
        for u in users:      # 예전 계정 역할(owner·member)은 일반회원(user)으로 저장한다
            u["role"] = account_role(u.get("role"))
            u.pop("requestedRole", None)
        with db.tx:
            db.USERS.save(users)

    def all(self):
        return self._read()             # 읽기만 하는 요청은 쓰기 차례를 기다리지 않는다

    def find(self, **kw):
        return next((u for u in self.all() if all(u.get(k) == v for k, v in kw.items())), None)

    def create(self, fields):
        with self.lock:
            users = self._read()
            if any(u["empNo"] == fields["empNo"] for u in users):
                raise ValidationError("이미 가입된 사번입니다.", HTTPStatus.CONFLICT)
            if any(u["email"].lower() == fields["email"].lower() for u in users):
                raise ValidationError("이미 사용 중인 이메일입니다.", HTTPStatus.CONFLICT)
            user = {"id": max([u["id"] for u in users], default=0) + 1, **fields}
            users.append(user)
            self._write(users)
            return user

    def update(self, user_id, fn):
        """fn(user, users) 가 사용자를 수정한다. 없으면 None."""
        with self.lock:
            users = self._read()
            for u in users:
                if u["id"] == user_id:
                    fn(u, users)
                    self._write(users)
                    return u
            return None


users_store = UserStore(USERS_FILE)
DUMMY_SALT, DUMMY_HASH = hash_password("dummy-password-for-timing")  # 없는 사번도 같은 시간이 걸리게 하는 용도
# 로그인 세션은 DB(sessions 표)에 둔다: 서버를 다시 켜도 로그인이 풀리지 않는다 (토큰은 해시로 저장. db.py 의 session_*)
fails = {}      # "IP|사번" -> (count, lock_until)  — 로그인 실패 횟수는 메모리에만 둔다
sess_lock = threading.Lock()


def public(u):
    d = {k: u.get(k, "") for k in ("id", "empNo", "name", "dept", "email", "role", "status", "createdAt", "approvedAt", "lastLoginAt")}
    d["mustChangePw"] = bool(u.get("mustChangePw", False))
    return d


def need(body, key, label, limit):
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{label}을(를) 입력하세요.")
    if len(v.strip()) > limit:
        raise ValidationError(f"{label}은(는) {limit}자 이하로 입력하세요.")
    return v.strip()


def check_password_rule(pw):
    if not isinstance(pw, str) or len(pw) < 8 or len(pw) > 64:
        raise ValidationError("비밀번호는 8~64자로 입력하세요.")
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)):
        raise ValidationError("비밀번호는 영문, 숫자, 특수문자를 모두 포함해야 합니다.")


class AuthMixin:
    """server.py 의 Handler 가 상속해서 쓰는 인증 기능.

    Handler 쪽에 _json(status, payload, cookie=None), _route(), _handle(fn) 이 있어야 한다.
    """

    # ---- 공통 검사 (다른 API 에서도 사용) ----
    def _check_origin(self):
        """다른 사이트에서 몰래 보낸 요청(CSRF)을 막는다: Origin 이 있으면 우리 주소(Host)와 같아야 한다."""
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != self.headers.get("Host"):
            raise ValidationError("허용되지 않은 요청입니다.", HTTPStatus.FORBIDDEN)

    def _auth_body(self):
        self._check_origin()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValidationError("요청이 너무 큽니다.")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValidationError("JSON 형식이 올바르지 않습니다.")
        if not isinstance(data, dict):
            raise ValidationError("JSON 형식이 올바르지 않습니다.")
        return data

    def _token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def _user(self):
        tok = self._token()
        s = db.session_get(tok)
        if not s or s["exp"] < time.time():
            if s:
                db.session_drop(tok)
            return None
        uid = s["uid"]
        u = users_store.find(id=uid)
        return u if u and u["status"] == "active" else None

    def _require(self, admin=False, allow_must_change=False):
        """로그인 확인. admin=True 면 사이트관리자만 통과."""
        u = self._user()
        if not u:
            raise ValidationError("로그인이 필요합니다.", HTTPStatus.UNAUTHORIZED)
        if u.get("mustChangePw") and not allow_must_change:
            raise ValidationError("임시 비밀번호입니다. 먼저 비밀번호를 변경하세요.", HTTPStatus.FORBIDDEN)
        if admin and u["role"] != "admin":
            raise ValidationError("사이트관리자만 사용할 수 있습니다.", HTTPStatus.FORBIDDEN)
        return u

    def _require_writer(self):
        """쓰기 요청: 로그인한 사용자(일반회원 포함)면 통과하고 Origin 을 검사한다.
        과제별 권한(과제책임자·과제참여자·사이트관리자)은 각 요청 처리에서 따로 확인한다."""
        self._check_origin()
        return self._require()

    def _cookie(self, token, max_age):
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"

    # ---- 라우팅: 처리했으면 True ----
    def auth_get(self, r):
        if r == "/api/auth/me":
            self._handle(lambda: self._json(HTTPStatus.OK, public(self._require(allow_must_change=True))))
        elif r == "/api/users":
            self._handle(lambda: (self._require(admin=True), self._json(HTTPStatus.OK, [public(u) for u in users_store.all()])))
        elif r == "/api/audit":
            self._handle(lambda: (self._require(admin=True), self._json(HTTPStatus.OK, read_audit())))
        elif r == "/api/users/directory":
            def directory():
                self._require()
                self._json(HTTPStatus.OK, [{k: u[k] for k in ("id", "empNo", "name", "dept", "role")}
                                           for u in users_store.all() if u["status"] == "active"])
            self._handle(directory)
        else:
            return False
        return True

    def auth_post(self, r):
        routes = {"/api/auth/signup": self._signup, "/api/auth/login": self._login,
                  "/api/auth/logout": self._logout, "/api/auth/password": self._change_pw}
        if r not in routes:
            return False
        self._handle(routes[r])
        return True

    def auth_patch(self, r):
        if not r.startswith("/api/users/"):
            return False
        self._handle(self._admin_action)
        return True

    # ---- 인증 ----
    def _signup(self):
        b = self._auth_body()
        emp = need(b, "empNo", "사번", 20)
        if not re.fullmatch(r"\d{6}", emp):
            raise ValidationError("사번은 6자리 숫자로 입력하세요.")
        email = need(b, "email", "이메일", 100)
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@lh\.or\.kr", email, re.IGNORECASE):
            raise ValidationError("이메일은 @lh.or.kr 주소만 사용할 수 있습니다.")
        email = email.lower()
        # 가입할 때 역할을 고르지 않는다: 가입자는 모두 일반회원(user). 사이트관리자는 가입으로 만들 수 없다.
        pw = b.get("password")
        check_password_rule(pw)
        salt, h = hash_password(pw)
        users_store.create({
            "empNo": emp, "name": need(b, "name", "성명", 50), "dept": need(b, "dept", "소속", 100), "email": email,
            "role": "user", "status": "pending", "salt": salt, "pwHash": h,
            "createdAt": now_iso(), "approvedAt": "", "lastLoginAt": "",
        })
        audit(emp, "signup", emp, "")
        self._json(HTTPStatus.CREATED, {"ok": True, "message": "가입 신청이 접수되었습니다. 사이트관리자 승인 후 로그인할 수 있습니다."})

    def _login(self):
        b = self._auth_body()
        emp, pw = b.get("empNo"), b.get("password")
        if not isinstance(emp, str) or not isinstance(pw, str):
            raise ValidationError("사번과 비밀번호를 입력하세요.")
        emp = emp.strip()
        # 잠금은 (접속 IP, 사번) 단위: 남이 내 사번으로 실패를 쌓아도 내 접속은 잠기지 않는다
        key = f"{self.client_address[0]}|{emp}"
        with sess_lock:
            _, until = fails.get(key, (0, 0))
            if until > time.time():
                raise ValidationError("로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.", HTTPStatus.TOO_MANY_REQUESTS)
        u = users_store.find(empNo=emp)
        # 없는 사번이어도 같은 해시 계산을 해서 응답 시간으로 사번 존재 여부를 알 수 없게 한다
        if u:
            ok = verify_password(pw, u["salt"], u["pwHash"])
        else:
            verify_password(pw, DUMMY_SALT, DUMMY_HASH)
            ok = False
        if not ok:
            with sess_lock:
                if len(fails) > FAIL_MAX_KEYS:
                    now = time.time()
                    for k in [k for k, (_, t) in fails.items() if t < now]:
                        fails.pop(k)
                cnt = fails.get(key, (0, 0))[0] + 1
                locked = cnt >= MAX_FAILS
                fails[key] = (cnt, time.time() + LOCK_SECONDS if locked else 0)
            if locked:
                audit(emp, "login_locked", emp, f"IP {self.client_address[0]}")
            raise ValidationError("사번 또는 비밀번호가 올바르지 않습니다.", HTTPStatus.UNAUTHORIZED)
        msgs = {"pending": "관리자 승인 대기 중인 계정입니다.", "rejected": "가입이 반려된 계정입니다. 관리자에게 문의하세요.",
                "inactive": "비활성화된 계정입니다. 관리자에게 문의하세요."}
        if u["status"] != "active":
            raise ValidationError(msgs[u["status"]], HTTPStatus.FORBIDDEN)
        with sess_lock:
            fails.pop(key, None)
            tok = secrets.token_urlsafe(32)
        db.session_put(tok, u["id"], time.time() + SESSION_TTL, time.time())
        u = users_store.update(u["id"], lambda x, _: x.update(lastLoginAt=now_iso()))
        self._json(HTTPStatus.OK, public(u), cookie=self._cookie(tok, SESSION_TTL))

    def _logout(self):
        self._check_origin()
        db.session_drop(self._token())
        self._json(HTTPStatus.OK, {"ok": True}, cookie=self._cookie("", 0))

    def _change_pw(self):
        u, b = self._require(allow_must_change=True), self._auth_body()
        if not isinstance(b.get("current"), str) or not verify_password(b["current"], u["salt"], u["pwHash"]):
            raise ValidationError("현재 비밀번호가 올바르지 않습니다.")
        check_password_rule(b.get("new"))
        if b["new"] == b["current"]:
            raise ValidationError("새 비밀번호는 현재 비밀번호와 달라야 합니다.")
        salt, h = hash_password(b["new"])
        users_store.update(u["id"], lambda x, _: (x.update(salt=salt, pwHash=h), x.pop("mustChangePw", None)))
        tok = self._token()
        db.session_drop_user(u["id"], keep_token=tok)   # 비밀번호를 바꾸면 이 기기를 뺀 다른 로그인은 모두 끊는다
        audit(u["empNo"], "password_change", u["empNo"])
        self._json(HTTPStatus.OK, {"ok": True})

    # ---- 관리자 작업 ----
    def _admin_action(self):
        me, b = self._require(admin=True), self._auth_body()
        try:
            uid = int(self._route().rsplit("/", 1)[1])
        except ValueError:
            raise ValidationError("사용자 ID가 올바르지 않습니다.")
        action = b.get("action")
        if action not in ("approve", "reject", "deactivate", "activate", "resetPassword"):   # 역할 변경(setRole)은 없다: 계정은 사이트관리자/일반회원뿐
            raise ValidationError("지원하지 않는 작업입니다.")
        target = users_store.find(id=uid)
        if not target:
            raise ValidationError("사용자를 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
        if action in ("approve", "reject") and target["status"] != "pending":
            raise ValidationError("승인 대기 상태의 사용자만 처리할 수 있습니다.")
        if action in ("deactivate", "resetPassword") and target["status"] in ("pending", "rejected"):
            raise ValidationError("승인된 사용자만 처리할 수 있습니다.")
        if action == "activate" and target["status"] != "inactive":
            raise ValidationError("비활성 상태의 사용자만 활성화할 수 있습니다.")
        out = {}

        def apply(u, users):
            # 마지막 사이트관리자는 비활성화할 수 없다 (본인 포함)
            admins = [x for x in users if x["role"] == "admin" and x["status"] == "active"]
            loses_admin = u["role"] == "admin" and u["status"] == "active" and action == "deactivate"
            if loses_admin and len(admins) <= 1:
                raise ValidationError("마지막 사이트관리자는 변경할 수 없습니다.")
            if u["id"] == me["id"] and action == "deactivate":
                raise ValidationError("본인 계정은 비활성화할 수 없습니다.")
            if action == "approve":
                u.update(status="active", approvedAt=now_iso())   # 승인하면 일반회원으로 사용 시작
            elif action == "reject":
                u["status"] = "rejected"
            elif action == "deactivate":
                u["status"] = "inactive"
            elif action == "activate":
                u["status"] = "active"
            elif action == "resetPassword":
                tmp = secrets.token_urlsafe(9) + "!1a"
                u["salt"], u["pwHash"] = hash_password(tmp)
                u["mustChangePw"] = True   # 첫 로그인 때 새 비밀번호로 바꿔야 다른 기능을 쓸 수 있다
                out["tempPassword"] = tmp
        res = users_store.update(uid, apply)
        audit(me["empNo"], action, target["empNo"], "")
        if action in ("deactivate", "resetPassword"):
            if uid != me["id"]:  # 상태·권한이 바뀐 사용자는 기존 세션을 끊는다
                db.session_drop_user(uid)
        self._json(HTTPStatus.OK, {**public(res), **out})
