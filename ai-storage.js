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
  let mode = 'server';

  const readLocal = () => { try { return JSON.parse(localStorage.getItem(LOCAL_KEY)) || []; } catch (e) { return []; } };
  const writeLocal = items => { localStorage.setItem(LOCAL_KEY, JSON.stringify(items)); };
  const nowIso = () => new Date().toISOString();

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

  // 서버 없이 처음 열었으면(브라우저 저장소에 키가 없으면) 더미데이터로 채운다
  async function readLocalOrDemo() {
    let raw = null;
    try { raw = localStorage.getItem(LOCAL_KEY); } catch (e) { return []; }
    if (raw !== null || typeof DemoData === 'undefined') return readLocal();
    try {
      const { aiProjects } = await DemoData.load();
      writeLocal(aiProjects);
      return aiProjects;
    } catch (e) { return []; }
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
      const item = { id: Date.now(), ...fields, createdAt: nowIso(), updatedAt: nowIso() };
      items.unshift(item); writeLocal(items);
      return item;
    },

    /** 기존 자료를 같은 id 로 덮어쓴다 (새 항목이 생기지 않음) */
    async update(id, fields) {
      if (mode === 'server') return request('PUT', `${API}/${id}`, fields);
      const items = readLocal();
      const it = items.find(x => x.id == id);
      if (!it) throw new Error('AI 과제를 찾을 수 없습니다.');
      Object.assign(it, fields, { updatedAt: nowIso() });
      writeLocal(items);
      return it;
    },

    async remove(id) {
      if (mode === 'server') return request('DELETE', `${API}/${id}`);
      writeLocal(readLocal().filter(x => x.id != id));
      return null;
    },
  };
})();
