/**
 * A01 인증 모듈 (Auth) — 모든 화면이 공유합니다. (msshinlh/auth.js 를 task-manager 에 맞게 고침)
 *
 *  - 이 파일을 불러오면 자동으로 로그인을 확인합니다. (로그인·회원가입 화면은 <script src="auth.js" data-guard="off">)
 *      · 서버(server.py)로 열었고 로그아웃 상태 → login.html 로 이동 (로그인 후 원래 화면으로 돌아옴)
 *      · 서버가 없을 때(GitHub Pages, 파일 더블클릭) → '디자인 미리보기' 모드: 로그인 없이 사이트관리자 화면으로 보여 줌
 *  - 로그인되면 기존 사이드바에 '내 정보'(+ 사이트관리자에게만 '사용자 관리'·'사이트 설정') 메뉴를, 머리글에 이름·역할·로그아웃을 붙입니다.
 *  - 과제참여자(member)에게는 class="w-only" 요소(등록·수정·삭제 버튼 등)를 숨깁니다. 실제 차단은 서버가 합니다.
 *
 *  Auth.guard({admin})  : 로그인 확인 결과(user)를 돌려주는 Promise. admin:true 면 사이트관리자가 아닐 때 대시보드로 이동
 *  Auth.user            : { id, empNo, name, dept, email, role, preview? }
 *  Auth.preview         : 디자인 미리보기 모드이면 true
 *  Auth.probe()         : 로그인 상태만 확인 { user } | { login: true } | { preview: true }
 *  Auth.api(method, url, body) : JSON API 호출 (실패하면 Error(서버 메시지), e.status)
 *  Auth.ROLE / Auth.STATUS     : 표시 이름·색상
 */
const Auth = (() => {
  const ROLE = { admin: ['사이트관리자', '#722ed1'], owner: ['과제책임자', '#1677ff'], member: ['과제참여자', '#52c41a'] };
  const STATUS = { pending: ['승인 대기', '#fa8c16'], active: ['사용 중', '#52c41a'], inactive: ['비활성', '#8c8c8c'], rejected: ['반려', '#ff4d4f'] };
  const PREVIEW_USER = { id: 0, empNo: '000000', name: '미리보기', dept: '디자인 확인용', email: '', role: 'admin', status: 'active', preview: true, mustChangePw: false };
  const HOME = 'dashboard.html';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tag = ([label, color]) => `<span class="tag" style="background:${color}">${label}</span>`;
  const page = () => location.pathname.split('/').pop() || 'index.html';
  const hang = () => new Promise(() => {});   // 다른 화면으로 이동하는 중: 이후 코드가 실행되지 않게 멈춤
  const guardOff = document.currentScript && document.currentScript.dataset.guard === 'off';
  let user = null, preview = false, ready = null;

  async function api(method, url, body) {
    const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
    const data = res.status === 204 ? null : await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data?.error || `요청 실패 (${res.status})`); e.status = res.status; throw e; }
    return data;
  }

  /** 서버가 있는지, 로그인했는지 확인한다 */
  async function probe() {
    if (location.protocol === 'file:') return { preview: true };
    try {
      const res = await fetch('api/auth/me', { cache: 'no-store' });
      if (res.ok) return { user: await res.json() };
      if (res.status === 401) return { login: true };
      return { preview: true };          // 404 등: 로그인 API 가 없는 곳(GitHub Pages)
    } catch (e) {
      return { preview: true };          // 서버에 연결할 수 없음
    }
  }

  /** 로그인 후 돌아갈 화면 (같은 사이트의 html 파일만 허용) */
  function nextPage() {
    const n = new URLSearchParams(location.search).get('next') || '';
    return /^[\w-]+\.html(\?[^#]*)?$/.test(n) && !/^(login|signup)\.html/.test(n) ? n : HOME;
  }

  function guard({ admin = false } = {}) {
    if (!ready) ready = (async () => {
      const r = await probe();
      if (r.login) { location.replace('login.html?next=' + encodeURIComponent(page() + location.search)); return hang(); }
      if (r.preview) { preview = true; user = PREVIEW_USER; } else user = r.user;
      // 임시 비밀번호로 로그인했으면 비밀번호를 바꾸기 전에는 내 정보 화면만 쓸 수 있다
      if (user.mustChangePw && page() !== 'mypage.html') { location.replace('mypage.html?force=1'); return hang(); }
      if (document.readyState === 'loading') await new Promise(res => document.addEventListener('DOMContentLoaded', res, { once: true }));
      decorate();
      return user;
    })();
    return ready.then(u => {
      if (admin && u.role !== 'admin') { location.replace(HOME); return hang(); }
      return u;
    });
  }

  async function logout() {
    try { await api('POST', 'api/auth/logout'); } catch (e) {}
    location.href = 'login.html';
  }

  const STYLE = `
    .auth-who{margin-left:auto;display:flex;align-items:center;gap:8px;font-weight:400;font-size:13px;color:rgba(0,0,0,.65);white-space:nowrap}
    header small~.auth-who{margin-left:16px}
    .auth-who .tag{display:inline-block;padding:0 8px;border-radius:4px;font-size:12px;line-height:22px;color:#fff}
    .auth-who button,.auth-who a.btn{font:inherit;font-size:13px;height:auto;padding:2px 10px;border:1px solid #d9d9d9;border-radius:6px;background:#fff;color:inherit;cursor:pointer;text-decoration:none}
    .auth-who button:hover,.auth-who a.btn:hover{border-color:#1677ff;color:#1677ff}
    aside .auth-grp{padding:14px 24px 4px;font-size:12px;color:rgba(255,255,255,.35)}
    html[data-role=member] .w-only{display:none!important}
    @media(max-width:640px){.auth-who .nm{display:none}}`;

  /** 기존 사이드바·머리글에 계정 정보를 붙인다 (화면마다 한 번) */
  function decorate() {
    const root = document.documentElement;
    root.dataset.role = user.role;
    if (preview) root.dataset.preview = '1';
    if (!document.getElementById('auth-style')) {
      const st = document.createElement('style'); st.id = 'auth-style'; st.textContent = STYLE; document.head.appendChild(st);
    }
    const aside = document.querySelector('aside');
    if (aside && !aside.querySelector('.auth-grp')) {
      const menu = [['mypage.html', '내 정보']];
      if (user.role === 'admin') menu.push(['admin-users.html', '사용자 관리'], ['settings.html', '사이트 설정']);
      aside.insertAdjacentHTML('beforeend', `<div class="auth-grp">계정</div>` +
        menu.map(([h, l]) => `<a href="${h}"${page() === h ? ' class="on"' : ''}>${l}</a>`).join(''));
    }
    const header = document.querySelector('header');
    if (header && !header.querySelector('.auth-who')) {
      header.insertAdjacentHTML('beforeend', preview
        ? `<span class="auth-who" title="서버 없이 열어 로그인 없이 보여 주는 화면입니다. 실제 로그인·저장은 server.py(8885)로 실행한 주소에서 확인하세요.">${tag(['디자인 미리보기', '#fa8c16'])}<a class="btn" href="login.html">로그인 화면</a></span>`
        : `<span class="auth-who"><span class="nm">${esc(user.name)}</span>${tag(ROLE[user.role] || [user.role, '#8c8c8c'])}<button type="button" id="btnLogout">로그아웃</button></span>`);
      const b = document.getElementById('btnLogout');
      if (b) b.onclick = logout;
    }
  }

  if (!guardOff) guard();

  return { api, guard, probe, logout, nextPage, esc, tag, ROLE, STATUS, get user() { return user; }, get preview() { return preview; } };
})();
