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
/**
 * 개발단계 · 활용상태 (TaskState) — 모든 화면이 같은 기준을 쓰도록 여기 한 곳에 둔다. (서버: server.py 의 dev_stage 와 과제 수정 규칙)
 *
 *   개발단계 (진행률로 정해짐, 직접 고르지 않음):  0% 기획중 / 1~99% 개발중 / 100% 개발완료
 *   활용상태:  개발진행중(100% 미만)
 *            → 신청대기중(100% 달성)
 *            → 게시검토중(과제책임자가 '게시 요청')
 *            → 게시활용중(사이트관리자가 '서비스 게시')
 *            → 활용종료(사이트관리자가 '활용 종료')
 */
const TaskState = (() => {
  const DEV = [['기획중', '#8c8c8c'], ['개발중', '#1677ff'], ['개발완료', '#52c41a']];
  const USE = [['개발진행중', '#1677ff'], ['신청대기중', '#fa8c16'], ['게시검토중', '#722ed1'], ['게시활용중', '#52c41a'], ['활용종료', '#8c8c8c']];
  const prog = t => Math.min(100, Math.max(0, +(t && t.progress) || 0));
  const devOf = t => { const p = prog(t); return p >= 100 ? '개발완료' : p > 0 ? '개발중' : '기획중'; };
  const useOf = t => t.useEnded ? '활용종료' : t.registered ? '게시활용중' : prog(t) < 100 ? '개발진행중' : t.pubRequested ? '게시검토중' : '신청대기중';
  return {
    DEV, USE, devOf, useOf,
    DEV_NAMES: DEV.map(x => x[0]), USE_NAMES: USE.map(x => x[0]),
    DEV_COLOR: Object.fromEntries(DEV), USE_COLOR: Object.fromEntries(USE),
    /** 마감을 따질 과제인가: 게시됐거나 활용이 끝난 과제는 마감 지연·임박에서 뺀다 */
    isOpen: t => !['게시활용중', '활용종료'].includes(useOf(t)),
  };
})();

const TaskStorage = (() => {
  const LOCAL_KEY = 'tasks-v1';
  const MIGRATED_KEY = 'tasks-v1-migrated';
  const VERSION_KEY = 'tasks-v1-demo-version';   // 어떤 더미데이터로 채웠는지 (demo-data.js 의 version)
  const API = 'api/tasks';
  let mode = 'server';

  // ---- localStorage (임시 저장) ----
  const readLocal = () => { try { return JSON.parse(localStorage.getItem(LOCAL_KEY)) || []; } catch (e) { return []; } };
  const writeLocal = tasks => { try { localStorage.setItem(LOCAL_KEY, JSON.stringify(tasks)); } catch (e) {} };

  // ---- 변경 이력 (임시 저장 모드용. 서버 모드는 server.py 가 같은 형식으로 기록) ----
  const PRI_L = { high: '높음', mid: '보통', low: '낮음' };
  const TRACK = [
    ['progress', '진행률', v => v + '%'],
    ['pubRequested', '게시 요청', v => v ? '요청' : '없음'],
    ['registered', '서비스 게시', v => v ? '게시' : '해제'],
    ['useEnded', '활용 종료', v => v ? '종료' : '활용 중'],
    ['approver', '승인자', v => v || '없음'],
    ['regReason', '게시 사유', v => v || '없음'],
    ['assignee', '과제책임자', v => v || '없음'],
    ['due', '마감일', v => v || '없음'],
    ['priority', '우선순위', v => PRI_L[v] || v],
    ['title', '과제명', v => v],
    ['memo', '메모', v => v || '없음'],
    ['participants', '과제참여자', v => v || '없음'],
  ];
  const norm = (o, k) => (k === 'progress' ? (o[k] ?? 0) : ['registered', 'pubRequested', 'useEnded'].includes(k) ? !!o[k]
    : k === 'participants' ? (o[k] || []).map(p => p.name).join(', ')   // 비교·표시는 이름 목록으로
    : (o[k] ?? ''));
  function describe(before, fields) {
    return TRACK
      .filter(([k]) => k in fields && norm(before, k) !== norm(fields, k))
      .map(([k, label, f]) => `${label}: ${f(norm(before, k))} → ${f(norm(fields, k))}`)
      .join(' / ');
  }
  // 작업한 사람: 로그인한 사용자 이름 (서버 없는 화면은 '미리보기')
  const actor = () => (typeof Auth !== 'undefined' && Auth.user && Auth.user.name) || '';
  const stamp = text => { const by = actor(); return by ? { at: new Date().toISOString(), text, by } : { at: new Date().toISOString(), text }; };

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
      writeLocal(demo.tasks);
      localStorage.setItem(VERSION_KEY, demo.version);
      localStorage.setItem(MIGRATED_KEY, 'demo');   // 더미데이터는 나중에 서버로 옮기지 않음
    } catch (e) {}
    return demo.tasks;
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
        // 서버와 같은 규칙: 진행률을 100% 아래로 내리면 게시 요청이 풀리고, 게시를 해제하면 활용 종료 표시도 지운다
        fields = { ...fields };
        if ('progress' in fields && fields.progress < 100 && t.pubRequested && !('pubRequested' in fields)) fields.pubRequested = false;
        if (fields.registered === false && t.useEnded && !('useEnded' in fields)) fields.useEnded = false;
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
