/**
 * AI 과제 저장 모듈 (AIStorage)
 *
 * - 서버(server.py)로 열었으면: /api/ai-projects 로 저장 → data/ai_projects.json 파일에 영구 보관
 * - 파일을 더블클릭해 열었거나 서버가 꺼져 있으면: 브라우저 localStorage 임시 저장 (mode === 'local')
 *   이때 브라우저 저장소가 비어 있으면 demo-data.json 더미데이터(demo-data.js)로 채웁니다. (GitHub Pages 디자인 확인용)
 * - auth.js 를 함께 불러오면 init() 이 로그인 확인(Auth.guard)을 먼저 기다립니다.
 *
 * 모든 메서드는 Promise 를 돌려줍니다.
 */
const AIStorage = (() => {
  const LOCAL_KEY = 'ai-projects-v1';
  const API = 'api/ai-projects';
  const VERSION_KEY = 'ai-projects-v1-demo-version';   // 어떤 더미데이터로 채웠는지 (demo-data.js 의 version)
  let mode = 'server';

  const readLocal = () => { try { return JSON.parse(localStorage.getItem(LOCAL_KEY)) || []; } catch (e) { return []; } };
  const writeLocal = items => { localStorage.setItem(LOCAL_KEY, JSON.stringify(items)); };
  const nowIso = () => new Date().toISOString();
  // 중복 기준: 명칭 + 과제책임자 (공백·영문 대소문자 무시) — 서버의 dup_key 와 같은 규칙
  const dupKey = f => [f.title, f.ownerName].map(v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase()).join('\u0000');
  const checkDup = (items, f, skipId) => {
    if (items.some(x => x.id != skipId && dupKey(x) === dupKey(f))) throw new Error(`이미 같은 명칭·과제책임자의 AI 과제가 있습니다. (${f.title} / ${f.ownerName})`);
  };

  // 서버 없는 화면: 과제 진행(storage.js 의 'tasks-v1')을 AI 과제와 1:1 로 맞춘다 — 서버의 sync_tasks_with_projects 와 같은 규칙
  const TASK_KEY = 'tasks-v1';
  const STAGE_TO_TASK = { '기획중': ['todo', 0], '승인요청': ['todo', 0], '개발중': ['doing', 30], '승인검토중': ['doing', 90], '활용중': ['done', 100], '활용종료': ['done', 100] };
  function mirrorTasks(projects) {
    let tasks;
    try { tasks = JSON.parse(localStorage.getItem(TASK_KEY)) || []; } catch (e) { return; }
    const key = (a, b) => String(a || '').trim() + '\u0000' + String(b || '').trim();
    const pids = new Set(projects.map(p => p.id)), byPid = new Map(), loose = new Map();
    tasks.forEach(t => {
      if (pids.has(t.projectId) && !byPid.has(t.projectId)) byPid.set(t.projectId, t);
      else { const k = key(t.title, t.assignee); if (!loose.has(k)) loose.set(k, []); loose.get(k).push(t); }
    });
    const now = new Date().toISOString(), keep = [];
    let nextId = Math.max(Date.now(), ...tasks.map(t => +t.id || 0)) + 1;
    projects.forEach(p => {
      let t = byPid.get(p.id);
      if (!t) { const c = loose.get(key(p.title, p.ownerName)); if (c && c.length) { t = c.shift(); t.projectId = p.id; } }
      if (!t) {
        const [s, progress] = STAGE_TO_TASK[p.stage] || ['todo', 0];
        t = { id: nextId++, projectId: p.id, title: p.title, s, date: String(p.createdAt || now).slice(0, 10), assignee: p.ownerName || '',
              due: '', priority: 'mid', memo: '', progress, history: [{ at: now, text: 'AI 과제와 함께 생성' }] };
      } else if (t.title !== p.title || t.assignee !== p.ownerName) {
        const parts = [];
        if (t.title !== p.title) parts.push(`과제명: ${t.title} → ${p.title}`);
        if (t.assignee !== p.ownerName) parts.push(`담당자: ${t.assignee || '없음'} → ${p.ownerName || '없음'}`);
        t.title = p.title; t.assignee = p.ownerName || '';
        t.history = [...(t.history || []), { at: now, text: parts.join(' / ') + ' (AI 과제 수정에 따라)' }].slice(-100);
      }
      keep.push(t);
    });
    keep.sort((a, b) => b.id - a.id);
    try { localStorage.setItem(TASK_KEY, JSON.stringify(keep)); } catch (e) {}
  }

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

  // 서버 없는 화면: 브라우저 저장소가 비었거나, 팀이 더미데이터(demo-data.json)를 바꿨으면 새 더미데이터로 다시 채운다.
  // (서버 없는 화면은 디자인 확인용이므로, 더미데이터가 바뀌면 이 브라우저에서 고친 내용은 새 더미데이터로 바뀝니다)
  async function readLocalOrDemo() {
    let raw = null, seen = null;
    try { raw = localStorage.getItem(LOCAL_KEY); seen = localStorage.getItem(VERSION_KEY); } catch (e) { return []; }
    if (typeof DemoData === 'undefined') return readLocal();
    let demo;
    try { demo = await DemoData.load(); } catch (e) { return readLocal(); }   // 파일로 열어 불러올 수 없으면 있는 그대로
    if (raw !== null && seen === demo.version) return readLocal();
    try {
      writeLocal(demo.aiProjects);
      localStorage.setItem(VERSION_KEY, demo.version);
    } catch (e) {}
    return demo.aiProjects;
  }

  return {
    get mode() { return mode; },

    async init() {
      if (typeof Auth !== 'undefined') await Auth.guard();   // 로그인 확인 (서버 없으면 미리보기 모드)
      if (location.protocol === 'file:') { mode = 'local'; return readLocalOrDemo(); }
      try {
        const items = await request('GET', API);
        mode = 'server';
        return items;
      } catch (e) {
        if (e.status === 401) { location.replace('login.html'); return new Promise(() => {}); }   // 로그인이 풀림
        mode = 'local';
        return readLocalOrDemo();
      }
    },

    async list() {
      return mode === 'server' ? request('GET', API) : readLocal();
    },

    /** fields: { title, purpose, category, stage, ownerName, ownerDept, access } */
    async create(fields) {
      if (mode === 'server') return request('POST', API, fields);
      const items = readLocal();
      checkDup(items, fields);
      const item = { id: Date.now(), ...fields, createdAt: nowIso(), updatedAt: nowIso() };
      items.unshift(item); writeLocal(items); mirrorTasks(items);
      return item;
    },

    /** 기존 자료를 같은 id 로 덮어쓴다 (새 항목이 생기지 않음) */
    async update(id, fields) {
      if (mode === 'server') return request('PUT', `${API}/${id}`, fields);
      const items = readLocal();
      const it = items.find(x => x.id == id);
      if (!it) throw new Error('AI 과제를 찾을 수 없습니다.');
      checkDup(items, fields, id);
      Object.assign(it, fields, { updatedAt: nowIso() });
      writeLocal(items); mirrorTasks(items);
      return it;
    },

    async remove(id) {
      if (mode === 'server') return request('DELETE', `${API}/${id}`);
      const rest = readLocal().filter(x => x.id != id);
      writeLocal(rest); mirrorTasks(rest);
      return null;
    },
  };
})();
