"""AI 과제 작업 기록 — 삭제·엑셀 올리기를 data/work_log.jsonl 에 한 줄(JSON)씩 남긴다.

server.py 가 불러 쓴다. '사이트 설정 → 작업 기록' 탭에서 사이트관리자가 본다 (GET /api/work-log).

한 줄의 모양:
    {"at": "2026-11-13T11:20:00+09:00", "actor": "100001", "actorName": "김하늘", "actorRole": "admin",
     "action": "ai_delete" | "ai_bulk_delete" | "ai_import", "count": 3, "file": "올리기.xlsx"(올리기만),
     "items": [{"id": ..., "title": "...", "ownerName": "...", "ownerDept": "..."}, ...]}
"""

import json
import threading
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "data" / "work_log.jsonl"
ACTIONS = {"ai_delete": "AI 과제 삭제", "ai_bulk_delete": "AI 과제 선택 삭제", "ai_import": "AI 과제 엑셀 올리기"}
MAX_ITEMS = 1000      # 한 기록에 남기는 과제 수 상한 (엑셀 올리기 최대 행 수와 같음)
_lock = threading.Lock()


def record(user, action, items, file_name=""):
    """user: 로그인 사용자(dict), items: AI 과제 목록. 비밀번호 등 민감한 값은 남기지 않는다."""
    rec = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor": user.get("empNo", ""), "actorName": user.get("name", ""), "actorRole": user.get("role", ""),
        "action": action, "count": len(items),
        "items": [{k: it.get(k, "") for k in ("id", "title", "ownerName", "ownerDept")} for it in items[:MAX_ITEMS]],
    }
    if file_name:
        rec["file"] = str(file_name)[:200]
    with _lock:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read(limit=500):
    """최근 기록부터 limit 건."""
    try:
        with _lock:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out
