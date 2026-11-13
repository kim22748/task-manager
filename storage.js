/**
 * 과제 저장 모듈 (TaskStorage)
 *
 * - 서버(server.py)로 열었으면: /api/tasks 로 저장 → data/tasks.json 파일에 영구 보관
 * - 파일을 더블클릭해 열었거나 서버가 꺼져 있으면: 브라우저 localStorage 임시 저장 (mode === 'local')
 *   이때 브라우저 저장소가 비어 있으면 demo-data.json 더미데이터(demo-data.js)로 채웁니다. (GitHub Pages 디자인 확인용)
 * - auth.js 를 함께 불러오면 init() 이 로그인 확인(Auth.guard)을 먼저 기다립니다.
 *
 * 모든 메서드는 Promise 를 돌려줍니다.
 */
const TaskStorage = (() => {
  const LOCAL_KEY = 'tasks-v1';
  const MIGRATED_KEY = 'tasks-v1-migrated';
  const API = 'api/tasks';
  let mode = 'server';

  // ---- localStorage (임시 저장) ----
  const readLocal = () => { try { return JSON.parse(localStorage.getItem(LOCAL_KEY)) || []; } catch (e) { return []; } };
  const writeLocal = tasks => { try { localStorage.setItem(LOCAL_KEY, JSON.stringify(tasks)); } catch (e) {} };

  // ---- 변경 이력 (임시 저장 모드용. 서버 모드는 server.py 가 같은 형식으로 기록) ----
  const STAGE = { todo: '검토', doing: '진행', done: '종료' };
  const PRI_L = { high: '높음', mid: '보통', low: '낮음' };
  const TRACK = [
    ['s', '단계', v => STAGE[v] || v],
    ['progress', '진행률', v => v + '%'],
    ['registered', '서비스 게시', v => v ? '게시' : '해제'],
    ['approver', '승인자', v => v || '없음'],
    ['regReason', '게시 사유', v => v || '없음'],
    ['assignee', '담당자', v => v || '없음'],
    ['due', '마감일', v => v || '없음'],
    ['priority', '우선순위', v => PRI_L[v] || v],
    ['title', '과제명', v => v],
    ['memo', '메모', v => v || '없음'],
  ];
  const norm = (o, k) => (k === 'progress' ? (o[k] ?? 0) : k === 'registered' ? !!o[k] : (o[k] ?? ''));
  function describe(before, fields) {
    return TRACK
      .filter(([k]) => k in fields && norm(before, k) !== norm(fields, k))
      .map(([k, label, f]) => `${label}: ${f(norm(before, k))} → ${f(norm(fields, k))}`)
      .join(' / ');
  }
  const stamp = text => ({ at: new Date().toISOString(), text });

  // ---- 서버 API ----
  async function request(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
    if (res.status === 204) return null;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data.error || `저장 실패 (${res.status})`); e.status = res.status; throw e; }
    return data;
  }

  // 서버 없이 처음 열었으면(브라우저 저장소에 과제 키가 없으면) 더미데이터로 채운다
  async function readLocalOrDemo() {
    let raw = null;
    try { raw = localStorage.getItem(LOCAL_KEY); } catch (e) { return []; }
    if (raw !== null || typeof DemoData === 'undefined') return readLocal();
    try {
      const { tasks } = await DemoData.load();
      writeLocal(tasks);
      try { localStorage.setItem(MIGRATED_KEY, 'demo'); } catch (e) {}   // 더미데이터는 나중에 서버로 옮기지 않음
      return tasks;
    } catch (e) { return []; }
  }

  // 예전에 localStorage 에만 저장돼 있던 과제를 서버로 한 번 옮긴다
  async function migrateLocal() {
    try { if (localStorage.getItem(MIGRATED_KEY)) return; } catch (e) { return; }
    const old = readLocal();
    if (old.length) await request('POST', `${API}/import`, { tasks: old });
    try { localStorage.setItem(MIGRATED_KEY, new Date().toISOString()); } catch (e) {}
  }

  return {
    get mode() { return mode; },

    /** 저장소 연결 후 과제 목록을 불러온다 */
    async init() {
      if (typeof Auth !== 'undefined') await Auth.guard();   // 로그인 확인 (서버 없으면 미리보기 모드)
      if (location.protocol === 'file:') { mode = 'local'; return readLocalOrDemo(); }
      try {
        await request('GET', API);
        mode = 'server';
        await migrateLocal();
        return await request('GET', API);
      } catch (e) {
        if (e.status === 401) { location.replace('login.html'); return new Promise(() => {}); }   // 로그인이 풀림
        mode = 'local';
        return readLocalOrDemo();
      }
    },

    async list() {
      return mode === 'server' ? request('GET', API) : readLocal();
    },

    /** fields: { title, assignee?, due?, priority?, memo?, progress? } (문자열만 넘기면 제목으로 처리) */
    async create(fields) {
      if (typeof fields === 'string') fields = { title: fields };
      if (mode === 'server') return request('POST', API, fields);
      const tasks = readLocal();
      const task = {
        id: Date.now(), title: fields.title, s: 'todo', date: new Date().toISOString().slice(0, 10),
        assignee: fields.assignee || '', due: fields.due || '', priority: fields.priority || 'mid', memo: fields.memo || '',
        progress: fields.progress || 0, history: [stamp('과제 등록')],
      };
      tasks.unshift(task); writeLocal(tasks);
      return task;
    },

    async update(id, fields) {
      if (mode === 'server') return request('PATCH', `${API}/${id}`, fields);
      const tasks = readLocal();
      const t = tasks.find(x => x.id == id);
      if (t) {
        const text = describe(t, fields);
        Object.assign(t, fields);
        if ('registered' in fields) {
          if (fields.registered) t.registeredAt = new Date().toISOString().slice(0, 10);
          else { t.registeredAt = ''; t.approver = ''; t.regReason = ''; }
        }
        if (text) t.history = [...(t.history || []), stamp(text)].slice(-100);
        writeLocal(tasks);
      }
      return t;
    },

    async remove(id) {
      if (mode === 'server') return request('DELETE', `${API}/${id}`);
      writeLocal(readLocal().filter(x => x.id != id));
      return null;
    },
  };
})();
