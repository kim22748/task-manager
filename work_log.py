"""AI 과제 작업 기록 — 삭제·엑셀 올리기를 DB(work_log 표, db.py)에 한 건씩 남긴다.

server.py 가 불러 쓴다. '사이트 설정 → 작업 기록' 탭에서 사이트관리자가 본다 (GET /api/work-log).

기록 한 건의 모양:
    {"at": "2026-11-13T11:20:00+09:00", "actor": "100001", "actorName": "김하늘", "actorRole": "admin",
     "action": "ai_delete" | "ai_bulk_delete" | "ai_import" | "category_add" | "category_rename" | "category_delete",
     "count": 3, "file": "올리기.xlsx"(올리기만), "detail": "분류 이름 변경: 개발 → 개발·SW"(분류 작업만),
     "items": [{"id": ..., "title": "...", "ownerName": "...", "ownerDept": "..."}, ...]}
"""

import json
import threading
from datetime import datetime
from pathlib import Path

import db

LOG_FILE = Path(__file__).resolve().parent / "data" / "work_log.jsonl"
ACTIONS = {"ai_delete": "AI 과제 삭제", "ai_bulk_delete": "AI 과제 선택 삭제", "ai_import": "AI 과제 엑셀 올리기",
           "category_add": "분류 추가", "category_rename": "분류 이름 변경", "category_delete": "분류 삭제",
           "task_cleanup": "과제 진행 정리"}
MAX_ITEMS = 1000      # 한 기록에 남기는 과제 수 상한 (엑셀 올리기 최대 행 수와 같음)

# 예전 기록 파일(data/work_log.jsonl)이 남아 있으면 처음 한 번 DB 로 옮긴다
db.adopt("work_log", LOG_FILE, lambda recs: [db.work_log_add(r) for r in recs if isinstance(r, dict)], load=db.legacy_lines)


def record(user, action, items, file_name="", detail=""):
    """user: 로그인 사용자(dict), items: AI 과제 목록. 비밀번호 등 민감한 값은 남기지 않는다."""
    rec = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor": user.get("empNo", ""), "actorName": user.get("name", ""), "actorRole": user.get("role", ""),
        "action": action, "count": len(items),
        "items": [{k: it.get(k, "") for k in ("id", "title", "ownerName", "ownerDept")} for it in items[:MAX_ITEMS]],
    }
    if file_name:
        rec["file"] = str(file_name)[:200]
    if detail:
        rec["detail"] = str(detail)[:300]
    db.work_log_add(rec)
    return rec


def read(limit=500):
    """최근 기록부터 limit 건."""
    return db.work_log_read(limit)
