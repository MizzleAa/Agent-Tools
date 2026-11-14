// 입력창의 '작업 위치' (관리자 · 새 대화일 때): 새 작업 폴더('날짜_시각_요청')를 어디 아래에 만들지.
// 값은 기준 폴더(환경변수 AGENT_WORK_ROOT, 없으면 서버의 workspace) 아래 상대 경로 · '' = 기준 폴더.
// 로컬 에이전트와 CLI · Claude Code 가 같은 값을 쓴다 (이 브라우저에 기억).
import { useState } from 'react';
import { localGet, localSet, useStored } from '@/lib/storage';
import { FolderPicker } from './FolderPicker';

const RECENT_KEY = 'work-bases';
const MAX_RECENT = 8;
const ABS = /^(?:[A-Za-z]:|[\\/])/;   // 예전에 저장한 전체 경로는 쓰지 않는다 (이제 상대 경로만)

function recents(): string[] {
  try { return (JSON.parse(localGet(RECENT_KEY) ?? '[]') as unknown[]).filter((x): x is string => typeof x === 'string' && Boolean(x) && !ABS.test(x)); } catch { return []; }
}

/** 고른 작업 위치 ('' = 기준 폴더) — ChatView·CcChatView 가 같이 쓴다 */
export function useWorkBase(): [string, (v: string) => void] {
  const [v, set] = useStored('work-base', '');
  return [ABS.test(v) ? '' : v, set];
}

export function BaseDirSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [list, setList] = useState(recents);
  const [picking, setPicking] = useState(false);
  const all = value && !list.includes(value) ? [value, ...list] : list;
  const remember = (p: string) => {
    if (!p) return;
    const next = [p, ...recents().filter((x) => x.toLowerCase() !== p.toLowerCase())].slice(0, MAX_RECENT);
    localSet(RECENT_KEY, JSON.stringify(next));
    setList(next);
  };
  return (
    <>
      <select className="ghost-select" value={value}
        title={value ? `새 작업 폴더를 기준 폴더 아래 '${value}' 에 만듭니다` : '새 작업 폴더를 기준 폴더(환경변수 AGENT_WORK_ROOT, 없으면 workspace) 바로 아래에 만듭니다'}
        onChange={(e) => { if (e.target.value === '__pick__') setPicking(true); else onChange(e.target.value); }}>
        <option value="">📁 기본 위치</option>
        {all.map((p) => <option key={p} value={p} title={p}>📁 {p}</option>)}
        <option value="__pick__">＋ 다른 작업 위치…</option>
      </select>
      {picking && <FolderPicker onClose={() => setPicking(false)} onPicked={(rel) => { remember(rel); onChange(rel); }} />}
    </>
  );
}
