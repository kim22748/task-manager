/**
 * AI 과제 저장 모듈 (AIStorage)
 *
 * - 서버(server.py)로 열었으면: /api/ai-projects 로 저장 → data/ai_projects.json 파일에 영구 보관
 * - 파일을 더블클릭해 열었거나 서버가 꺼져 있으면: 브라우저 localStorage 임시 저장 (mode === 'local')
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
    if (!res.ok) throw new Error(data.error || `저장 실패 (${res.status})`);
    return data;
  }

  return {
    get mode() { return mode; },

    async init() {
      if (location.protocol === 'file:') { mode = 'local'; return readLocal(); }
      try {
        const items = await request('GET', API);
        mode = 'server';
        return items;
      } catch (e) {
        mode = 'local';
        return readLocal();
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
