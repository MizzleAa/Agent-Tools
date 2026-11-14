// 설정 탭들이 같이 쓰는 것
import { createContext, type RefObject, useContext } from 'react';
import type { Schemas } from '@/api';
import { insertAt } from '@/ui/form';

export type Bundle = Schemas['SettingsBundle'];

/** 설정 화면 데이터 + 다시 읽기 */
export const SettingsCtx = createContext<{ data: Bundle; reload: () => Promise<void> } | null>(null);
export function useSettings() {
  const v = useContext(SettingsCtx);
  if (!v) throw new Error('SettingsCtx 밖');
  return v;
}

export function StateBadges({ modified, custom }: { modified?: boolean | null; custom?: boolean }) {
  return <>
    {modified && <span className="badge warn">수정됨</span>}
    {custom && <span className="badge">직접 만듦</span>}
  </>;
}

/** 누르면 입력칸 커서 자리에 {{변수}} 를 넣는 단추들 */
export function VarChips({ target, onChange, extra = [] }: {
  target: RefObject<HTMLTextAreaElement | null>; onChange: (v: string) => void; extra?: [string, string][];
}) {
  const { data } = useSettings();
  const names: [string, string][] = [...extra, ...data.builtin_vars.map((v): [string, string] => [v.name, v.description]),
    ...data.variables.map((v): [string, string] => [v.name, v.description || v.value])];
  return (
    <div className="var-chips">
      <span className="muted small">변수 넣기:</span>
      {names.map(([n, desc]) => (
        <button key={n} type="button" className="chip chip-btn" title={desc} onClick={() => insertAt(target.current, `{{${n}}}`, onChange)}>{`{{${n}}}`}</button>
      ))}
    </div>
  );
}
