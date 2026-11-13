/**
 * 시연용 더미데이터 모듈 (DemoData) — demo-data.json 을 오늘 날짜 기준으로 바꿔 돌려줍니다.
 *
 * - 서버 없이 열었을 때(GitHub Pages 등) 브라우저 저장소가 비어 있으면 storage.js / ai-storage.js 가 이 데이터를 씁니다.
 * - 서버(server.py)는 같은 규칙의 demo_items() 로 data/ 파일을 채웁니다. 한쪽을 고치면 다른 쪽도 고쳐 주세요.
 *
 *   await DemoData.load()  → { tasks, aiProjects, users, version }
 *   version 은 demo-data.json 내용의 지문(해시)입니다. 팀이 더미데이터를 바꾸면 값이 달라지고,
 *   storage.js / ai-storage.js 가 이를 보고 서버 없는 화면(GitHub Pages)의 브라우저 저장 데이터를 새 더미데이터로 다시 채웁니다.
 */
const DemoData = (() => {
  let cache = null;
  const pad = n => String(n).padStart(2, '0');
  const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  function build(demo) {
    const now = new Date();
    const day = n => { const d = new Date(now); d.setDate(d.getDate() - n); return d; };
    const atDate = (n, h) => { const d = day(n); d.setHours(h, 0, 0, 0); return d; };
    const at = (n, h) => atDate(n, h).toISOString();

    const tasks = demo.tasks.map((d, i) => {
      const n = d.daysAgo, s = d.s, prog = d.progress || 0;
      const history = [{ at: at(n, 10), text: '과제 등록' }];
      if (s !== 'todo') history.push({ at: at(Math.floor(n * 0.8), 11), text: '단계: 검토 → 진행' });
      if (prog) history.push({ at: at(Math.floor(n * 0.5), 14), text: `진행률: 0% → ${prog}%` });
      if (s === 'done') history.push({ at: at(Math.floor(n * 0.3), 16), text: '단계: 진행 → 종료' });
      const t = {
        id: atDate(n, 10).getTime() + i, title: d.title, s, date: ymd(day(n)),
        assignee: d.assignee || '', due: 'dueInDays' in d ? ymd(day(-d.dueInDays)) : '',
        priority: d.priority || 'mid', memo: d.memo || '', progress: prog,
      };
      if (d.registered) {
        const r = d.registeredDaysAgo || 0;
        history.push({ at: at(r, 17), text: `서비스 게시: 해제 → 게시 / 승인자: 없음 → ${d.approver} / 게시 사유: 없음 → ${d.regReason}` });
        Object.assign(t, { registered: true, approver: d.approver, regReason: d.regReason, registeredAt: ymd(day(r)) });
      }
      t.history = history;
      return t;
    }).sort((a, b) => b.id - a.id);

    const aiProjects = demo.aiProjects.map((d, i) => {
      const created = at(d.daysAgo, 10);
      const { title, purpose, category, stage, ownerName, ownerDept, access } = d;
      return { id: atDate(d.daysAgo, 10).getTime() + i, title, purpose, category, stage, ownerName, ownerDept, access, createdAt: created, updatedAt: created };
    }).sort((a, b) => b.id - a.id);

    // 화면 표시용 사용자 (비밀번호 없음) — 서버의 /api/users 응답과 같은 모양
    const users = demo.users.map((d, i) => ({
      id: i + 1, empNo: d.empNo, name: d.name, dept: d.dept, email: `user${d.empNo}@lh.or.kr`, role: d.role,
      requestedRole: d.requestedRole || 'member', status: d.status, createdAt: at(d.daysAgo || 0, 10),
      approvedAt: ['active', 'inactive'].includes(d.status) ? at(d.daysAgo || 0, 10) : '', lastLoginAt: '', mustChangePw: false,
    }));

    return { tasks, aiProjects, users };
  }

  async function load() {
    if (cache) return cache;
    const res = await fetch('demo-data.json', { cache: 'no-store' });
    if (!res.ok) throw new Error(`더미데이터를 불러오지 못했습니다 (${res.status})`);
    const text = await res.text();
    let h = 0x811c9dc5;                                   // FNV-1a 32bit: 내용이 바뀌었는지만 알면 되므로 간단한 해시
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    cache = { ...build(JSON.parse(text)), version: h.toString(16) };
    return cache;
  }

  return { load, build };
})();
