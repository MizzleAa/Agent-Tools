import type { ReactNode } from 'react';
import { useApp } from '@/state/AppState';
import { useUi } from '@/ui/UiProvider';

export function Card({ title, right, children, wide = false }: { title: string; right?: ReactNode; children?: ReactNode; wide?: boolean }) {
  return (
    <div className={`card${wide ? ' wide' : ''}`}>
      <div className="card-head"><span className="title">{title}</span><span className="spacer" />{right}</div>
      {children}
    </div>
  );
}

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return <dl className="kv">{rows.map(([k, v]) => [<dt key={`${k}-k`}>{k}</dt>, <dd key={`${k}-v`}>{v}</dd>])}</dl>;
}

/** 버튼 동작: 실패하면 알림, 끝나면 상태를 다시 읽는다. 성공 여부를 돌려준다 */
export function useAction() {
  const ui = useUi();
  const { refreshStatus } = useApp();
  return async (fn: () => Promise<unknown>): Promise<boolean> => {
    try { await fn(); return true; } catch (e) { ui.error(e); return false; } finally { setTimeout(refreshStatus, 400); }
  };
}
