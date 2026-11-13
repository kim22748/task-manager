/**
 * 첨부 파일 저장 모듈 (FileStorage) — storage.js(TaskStorage) 다음에 불러옵니다.
 *
 * - 서버 모드: /api/tasks/<id>/files 로 올리고 내려받음 → task-manager/data/files/ 에 보관
 * - 임시 저장 모드(GitHub Pages·파일 직접 열기): 이 브라우저의 IndexedDB 에만 보관
 *   (다른 PC·다른 브라우저에서는 보이지 않음)
 *
 * PRD 필수 첨부 3종은 확장자로만 확인합니다(내용은 보지 않음).
 */
const FileStorage = (() => {
  const MAX_FILE = 20 * 1024 * 1024;
  const REQUIRED = [
    { kind: '과제기획서', ext: ['.hwpx'], label: '과제기획서 (hwpx)' },
    { kind: '과제실행 파일', ext: ['.html', '.htm'], label: '과제실행 파일 (html)' },
    { kind: '과제활용 데이터', ext: ['.csv'], label: '과제활용 데이터 (csv)' },
  ];
  const kindOf = name => {
    const ext = (String(name).match(/\.[^.]+$/) || [''])[0].toLowerCase();
    const r = REQUIRED.find(x => x.ext.includes(ext));
    return r ? r.kind : '기타';
  };
  const isServer = () => typeof TaskStorage !== 'undefined' && TaskStorage.mode === 'server';
  const api = id => `api/tasks/${encodeURIComponent(id)}/files`;

  // ---- IndexedDB (임시 저장 모드) ----
  let dbp;
  function db() {
    if (!dbp) dbp = new Promise((resolve, reject) => {
      if (!window.indexedDB) return reject(new Error('이 브라우저는 파일 임시 저장을 지원하지 않습니다.'));
      const req = indexedDB.open('taskflow-files', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('files', { keyPath: 'id' }).createIndex('taskId', 'taskId');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error('파일 저장소를 열지 못했습니다.'));
    });
    return dbp;
  }
  async function tx(mode, fn) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const t = d.transaction('files', mode), store = t.objectStore('files');
      const req = fn(store);
      t.oncomplete = () => resolve(req && req.result);
      t.onerror = () => reject(t.error || new Error('파일 저장에 실패했습니다.'));
      t.onabort = () => reject(t.error || new Error('파일 저장이 취소되었습니다. (브라우저 저장 공간 부족일 수 있습니다)'));
    });
  }
  const strip = f => ({ id: f.id, name: f.name, size: f.size, kind: f.kind, uploadedAt: f.uploadedAt });
  // 임시 저장 모드에서도 과제 변경 이력에 남긴다 (storage.js 와 같은 localStorage 키)
  function localHistory(taskId, text) {
    try {
      const tasks = JSON.parse(localStorage.getItem('tasks-v1')) || [];
      const t = tasks.find(x => x.id == taskId);
      if (!t) return;
      t.history = [...(t.history || []), { at: new Date().toISOString(), text }].slice(-100);
      localStorage.setItem('tasks-v1', JSON.stringify(tasks));
    } catch (e) {}
  }

  async function errorOf(res) {
    const data = await res.json().catch(() => ({}));
    return new Error(data.error || `요청 실패 (${res.status})`);
  }

  return {
    REQUIRED, MAX_FILE, kindOf,

    /** 과제 하나의 첨부 파일 목록 [{id,name,size,kind,uploadedAt}] */
    async list(taskId) {
      if (isServer()) {
        const res = await fetch(api(taskId), { cache: 'no-store' });
        if (!res.ok) throw await errorOf(res);
        return res.json();
      }
      const all = await tx('readonly', s => s.index('taskId').getAll(String(taskId)));
      return (all || []).map(strip);
    },

    /** 여러 과제의 첨부 목록을 한 번에: { [taskId]: [meta...] } */
    async listMany(tasks) {
      if (isServer()) return Object.fromEntries(tasks.map(t => [String(t.id), t.files || []]));
      const all = await tx('readonly', s => s.getAll()).catch(() => []);
      const out = {};
      (all || []).forEach(f => (out[f.taskId] = out[f.taskId] || []).push(strip(f)));
      return out;
    },

    async upload(taskId, file) {
      if (file.size > MAX_FILE) throw new Error(`'${file.name}'은(는) 20MB를 넘어 올릴 수 없습니다.`);
      if (isServer()) {
        const res = await fetch(`${api(taskId)}?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: file });
        if (!res.ok) throw await errorOf(res);
        return res.json();
      }
      const rec = {
        id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, ''),
        taskId: String(taskId), name: file.name, size: file.size, kind: kindOf(file.name),
        uploadedAt: new Date().toISOString().slice(0, 19), blob: file,
      };
      await tx('readwrite', s => s.put(rec));
      localHistory(taskId, `파일 첨부: ${file.name}`);
      return strip(rec);
    },

    /** 파일 내려받기 (브라우저 저장 대화상자) */
    async download(taskId, meta) {
      const a = document.createElement('a');
      a.download = meta.name;
      if (isServer()) {
        a.href = `${api(taskId)}/${encodeURIComponent(meta.id)}`;
        a.click();
        return;
      }
      const rec = await tx('readonly', s => s.get(meta.id));
      if (!rec) throw new Error('파일을 찾을 수 없습니다.');
      a.href = URL.createObjectURL(rec.blob);
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    },

    async remove(taskId, meta) {
      if (isServer()) {
        const res = await fetch(`${api(taskId)}/${encodeURIComponent(meta.id)}`, { method: 'DELETE' });
        if (!res.ok && res.status !== 204) throw await errorOf(res);
        return;
      }
      await tx('readwrite', s => s.delete(meta.id));
      localHistory(taskId, `파일 삭제: ${meta.name}`);
    },

    /** PRD 필수 첨부 3종 확인 결과: [{label, ok}] */
    checkRequired(files) {
      return REQUIRED.map(r => ({ label: r.label, kind: r.kind, ok: files.some(f => f.kind === r.kind) }));
    },
  };
})();
