"""유사과제 탐색 시연용 목(가상) AI 과제를 서버의 'AI 과제 목록'에 넣는다.

사용:
    python load_demo_data.py [서버주소] [사번]          예) python load_demo_data.py http://localhost:8885 100001
    python load_demo_data.py http://localhost:8885 100001 --all    (목 데이터 100건을 모두 넣기)

- 비밀번호는 실행하면 물어본다. 입력해도 화면에 보이지 않고, 파일·명령줄에 남지 않는다.
- 기본은 demo-similar-result.json 이 가리키는 과제 중 demo-mock-ai-projects.json 에 있는 것만 넣는다.
  지금의 시연 결과는 AI 과제 목록의 시연 데이터(demo-data.json) 100건을 묶은 것이라 기본 실행으로는 넣을 것이 거의 없다.
  비교 과제를 늘려 보려면 --all 로 목 데이터 100건을 넣는다(시연 결과에 없어 '단독'으로 표시된다).
- 이미 같은 명칭의 AI 과제가 있으면 건너뛰므로 여러 번 실행해도 중복되지 않는다.
- 목 데이터는 실제 과제·인물이 아닌 가상 더미이며, API 키와 무관하다.
"""

import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = [a for a in sys.argv[1:] if not a.startswith("--")]
load_all = "--all" in sys.argv
base = (args[0] if args else "http://localhost:8885").rstrip("/")
# 윈도우에서 'localhost'는 IPv6(::1)를 먼저 시도해 요청마다 2초씩 늦어지므로 IPv4 주소로 바꿔 쓴다 (서버는 127.0.0.1 에서 받는다)
base = base.replace("//localhost", "//127.0.0.1")
emp_no = args[1] if len(args) > 1 else input("사번(사이트관리자 또는 과제책임자): ").strip()
# 직접 실행하면 화면에 보이지 않게 묻고, 다른 프로그램이 표준입력으로 넘겨 주는 경우(자동 시험 등)에는 한 줄을 읽는다
password = getpass.getpass("비밀번호(입력해도 보이지 않습니다): ") if sys.stdin.isatty() else sys.stdin.readline().strip()

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Origin": base})
    try:
        with opener.open(req, timeout=20) as res:
            text = res.read().decode("utf-8")
            return res.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text
    except urllib.error.URLError as e:
        sys.exit(f"서버에 연결하지 못했습니다({base}). 먼저 python server.py 로 서버를 켜세요. ({e.reason})")


status, me = call("POST", "/api/auth/login", {"empNo": emp_no, "password": password})
password = None
if status != 200:
    sys.exit(f"로그인하지 못했습니다: {me.get('error') if isinstance(me, dict) else me}")
print("로그인:", me.get("name"), f"({me.get('role')})")

mock = json.load(open(HERE / "demo-mock-ai-projects.json", encoding="utf-8"))["items"]
if not load_all:
    result = json.load(open(HERE / "demo-similar-result.json", encoding="utf-8"))
    wanted = {t for g in result["groups"] for t in g["task_titles"]} | set(result.get("ungrouped_titles", []))
    mock = [m for m in mock if m["title"] in wanted]

_, have = call("GET", "/api/ai-projects")
have_titles = {i["title"] for i in have} if isinstance(have, list) else set()
FIELDS = ("title", "purpose", "category", "stage", "ownerName", "ownerDept", "access")
added = skipped = 0
for item in mock:
    if item["title"] in have_titles:
        skipped += 1
        continue
    st, res = call("POST", "/api/ai-projects", {k: item[k] for k in FIELDS})
    if st == 201:
        added += 1
    else:
        print("  실패:", item["title"], st, res.get("error") if isinstance(res, dict) else res)
_, after = call("GET", "/api/ai-projects")
print(f"추가 {added}건 · 이미 있어 건너뜀 {skipped}건 · 지금 AI 과제 {len(after) if isinstance(after, list) else '?'}건")
