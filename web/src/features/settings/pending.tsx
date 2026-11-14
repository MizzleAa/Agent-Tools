// 설정 화면의 '저장하지 않은 변경'. 탭들이 바꾼 것을 여기 모아 두고 아래 저장 막대에서 한 번에 저장한다.
//   const [draft, update] = useDraft(초기값, 'profile:coder', 'coder 프로필', (v) => api…(v));
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useUi } from '@/ui/UiProvider';

interface PendingItem { label: string; save: () => Promise<unknown> }
interface Pending {
  items: Map<string, PendingItem>;
  touch: (key: string, label: string, save: () => Promise<unknown>) => void;
  drop: (key: string) => void;
  clear: () => void;
  saveAll: () => Promise<boolean>;
  /** 저장 안 한 게 있으면 버릴지 묻는다. 계속해도 되면 true */
  guard: () => Promise<boolean>;
}

const Ctx = createContext<Pending | null>(null);

export function usePending(): Pending {
  const v = useContext(Ctx);
  if (!v) throw new Error('PendingProvider 밖');
  return v;
}

export function PendingProvider({ children, onSaved }: { children: ReactNode; onSaved: () => Promise<void> }) {
  const ui = useUi();
  const [items, setItems] = useState(new Map<string, PendingItem>());
  const ref = useRef(items);
  ref.current = items;

  const touch = useCallback((key: string, label: string, save: () => Promise<unknown>) => {
    setItems((m) => new Map(m).set(key, { label, save }));
  }, []);
  const drop = useCallback((key: string) => setItems((m) => { const n = new Map(m); n.delete(key); return n; }), []);
  const clear = useCallback(() => setItems(new Map()), []);

  const saveAll = useCallback(async () => {
    for (const [key, item] of ref.current) {
      try { await item.save(); } catch (e) { ui.error(e); return false; }
      drop(key);
    }
    ui.toast('저장했습니다. 다음 실행부터 적용됩니다.', 'ok');
    await onSaved();
    return true;
  }, [drop, onSaved, ui]);

  const guard = useCallback(async () => {
    if (!ref.current.size) return true;
    const ok = await ui.confirm({ title: '저장하지 않은 변경이 있습니다', message: '저장하지 않고 이동하면 바꾼 내용이 사라집니다.', confirmText: '버리고 이동', danger: true });
    if (ok) clear();
    return ok;
  }, [clear, ui]);

  useEffect(() => {   // 창을 닫거나 새로고침할 때
    if (!items.size) return;
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener('beforeunload', onUnload);
    return () => window.removeEventListener('beforeunload', onUnload);
  }, [items.size]);

  useEffect(() => {   // Ctrl+S = 저장
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (ref.current.size) saveAll();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [saveAll]);

  const value = useMemo(() => ({ items, touch, drop, clear, saveAll, guard }), [items, touch, drop, clear, saveAll, guard]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function SaveBar({ onDiscard }: { onDiscard: () => void }) {
  const { items, saveAll, clear } = usePending();
  if (!items.size) return null;
  const labels = [...items.values()].map((x) => x.label).join(', ');
  return (
    <div className="savebar">
      <span className="msg"><b>저장하지 않은 변경 {items.size}건</b><span className="muted small"> · {labels}</span></span>
      <button type="button" className="btn" onClick={() => { clear(); onDiscard(); }}>되돌리기</button>
      <button type="button" className="btn primary" title="Ctrl+S" onClick={saveAll}>저장</button>
    </div>
  );
}

/** 고치는 중인 값 + 바뀔 때마다 저장할 것으로 올리기 */
export function useDraft<T>(initial: T, key: string, label: string, save: (v: T) => Promise<unknown>): [T, (next: T) => void] {
  const { touch } = usePending();
  const [draft, setDraft] = useState<T>(initial);
  const update = useCallback((next: T) => {
    setDraft(next);
    touch(key, label, () => save(next));
  }, [key, label, save, touch]);
  return [draft, update];
}
