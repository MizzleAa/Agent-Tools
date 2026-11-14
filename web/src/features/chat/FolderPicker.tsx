// 작업 위치 고르기 (관리자): 기준 폴더(환경변수 AGENT_WORK_ROOT, 없으면 서버 아래 작업 폴더 workspace) 아래를 상대 경로로 고른다.
// 없는 폴더 이름을 적어도 된다 — 보낼 때 만든다. 입력창 '작업 위치' · 관리 › 외부 AI 모델 › CLI 의 작업 폴더 추가가 같이 쓴다.
import { useEffect, useState } from 'react';
import { api, ApiError, errorText, type Schemas } from '@/api';
import { Icon, Spinner } from '@/ui/Icon';
import { Modal } from '@/ui/Modal';

/** 기준 폴더 + 상대 경로 → 서버 PC 의 전체 경로 (보여 주기·작업 폴더 추가용) */
export const joinRoot = (root: string, rel: string) => (rel ? `${root.replace(/[\\/]+$/, '')}\\${rel.replace(/\//g, '\\')}` : root);

export function FolderPicker({ title = '작업 위치 고르기', help, confirmText = '이 위치로', onClose, onPicked }: {
  title?: string; help?: string; confirmText?: string; onClose: () => void; onPicked: (rel: string, full: string) => void;
}) {
  const [list, setList] = useState<Schemas['FolderList'] | null>(null);
  const [path, setPath] = useState('');
  const [err, setErr] = useState('');
  const [missing, setMissing] = useState(false);
  const go = async (p?: string) => {
    setErr('');
    setMissing(false);
    try {
      const r = await api.admin.dirs(p);
      setList(r);
      setPath(r.path);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setMissing(true);   // 아직 없는 폴더 — 골라도 된다 (보낼 때 만든다)
      else setErr(errorText(e));
    }
  };
  useEffect(() => { void go(); }, []);
  const rel = path.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
  return (
    <Modal onClose={onClose} wide labelledBy="folder-picker-title">
      <div className="modal-title" id="folder-picker-title">{title}</div>
      <div className="modal-body cc-folder">
        <div className="muted small">{help ?? '고른 위치 아래에 새 작업마다 \'날짜_시각_요청\' 폴더를 만들어 그 안에서 작업합니다.'}</div>
        {list && (
          <div className="small">기준 폴더 <code className="inline">{list.root}</code>
            <span className="muted"> {list.root_from_env ? '(환경변수 AGENT_WORK_ROOT)' : '(서버의 작업 폴더 — 환경변수 AGENT_WORK_ROOT 로 바꿀 수 있음)'}</span></div>
        )}
        <div className="inline-add">
          <input type="text" value={path} placeholder="기준 아래 상대 경로 (예: 앱/로그인) — 비우면 기준 폴더" onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void go(path); }} />
          <button type="button" className="btn" onClick={() => go(path)}>열기</button>
        </div>
        {missing && <div className="muted small">아직 없는 폴더입니다 — '{confirmText}'를 누르면 보낼 때 만듭니다.</div>}
        {err && <div className="notice error"><span className="msg">{err}</span></div>}
        <div className="cc-dirs">
          {!list && !err && <Spinner />}
          {list && list.parent !== null && (
            <button type="button" className="cc-dir" onClick={() => go(list.parent ?? '')}><Icon name="chevron" size={14} className="cc-up" /> 위로</button>
          )}
          {list?.dirs.map((d) => (
            <button key={d.path} type="button" className="cc-dir" onClick={() => go(d.path)} title={d.path}><Icon name="folder" size={15} /> {d.name}</button>
          ))}
          {list && list.dirs.length === 0 && <div className="muted small">하위 폴더가 없습니다.</div>}
        </div>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn" onClick={onClose}>취소</button>
        <button type="button" className="btn primary" disabled={!list || Boolean(err)} onClick={() => { onPicked(rel, joinRoot(list!.root, rel)); onClose(); }}>
          {confirmText}{rel ? ` (${rel})` : ' (기준 폴더)'}</button>
      </div>
    </Modal>
  );
}
