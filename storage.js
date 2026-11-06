/**
 * 과제 저장 모듈 (TaskStorage)
 *
 * - 서버(server.py)로 열었으면: /api/tasks 로 저장 → data/tasks.json 파일에 영구 보관
 * - 파일을 더블클릭해 열었거나 서버가 꺼져 있으면: 브라우저 localStorage 임시 저장 (mode === 'local')
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
    if (!res.ok) throw new Error(data.error || `저장 실패 (${res.status})`);
    return data;
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
      if (location.protocol === 'file:') { mode = 'local'; return readLocal(); }
      try {
        await request('GET', API);
        mode = 'server';
        await migrateLocal();
        return await request('GET', API);
      } catch (e) {
        mode = 'local';
        return readLocal();
      }
    },

    async list() {
      return mode === 'server' ? request('GET', API) : readLocal();
    },

    /** fields: { title, assignee?, due?, priority?, memo? } (문자열만 넘기면 제목으로 처리) */
    async create(fields) {
      if (typeof fields === 'string') fields = { title: fields };
      if (mode === 'server') return request('POST', API, fields);
      const tasks = readLocal();
      const task = {
        id: Date.now(), title: fields.title, s: 'todo', date: new Date().toISOString().slice(0, 10),
        assignee: fields.assignee || '', due: fields.due || '', priority: fields.priority || 'mid', memo: fields.memo || '',
      };
      tasks.unshift(task); writeLocal(tasks);
      return task;
    },

    async update(id, fields) {
      if (mode === 'server') return request('PATCH', `${API}/${id}`, fields);
      const tasks = readLocal();
      const t = tasks.find(x => x.id == id);
      if (t) { Object.assign(t, fields); writeLocal(tasks); }
      return t;
    },

    async remove(id) {
      if (mode === 'server') return request('DELETE', `${API}/${id}`);
      writeLocal(readLocal().filter(x => x.id != id));
      return null;
    },
  };
})();
