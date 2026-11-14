// 실행 파라미터: config/settings.yaml 의 값 (주석은 그대로 두고 값만 바꾼다)
import { api, type Schemas } from '@/api';
import { Field, Section } from '@/ui/form';
import { useDraft } from './pending';
import { useSettings } from './parts';

const show = (v: unknown) => (v == null || v === '' ? '없음' : Array.isArray(v) ? v.join(', ') : String(v));

export function ParamsTab() {
  const { data } = useSettings();
  return (
    <div className="set-stack">
      <div className="muted set-intro">config/settings.yaml 의 값을 고칩니다. 주석은 그대로 두고 값만 바꾸며, 저장하면 다음 실행부터 적용됩니다 (컨텍스트 길이는 모델 서버를 다시 켜야 적용). 바꾸기 전 내용은 '변경 기록'에 남습니다.</div>
      {Object.entries(data.params.sections).map(([sec, spec]) => <ParamSection key={sec} sec={sec} spec={spec} />)}
    </div>
  );
}

function ParamSection({ sec, spec }: { sec: string; spec: Schemas['ParamSection'] }) {
  const { data } = useSettings();
  const P = data.params;
  const fileVals = P.file[sec] ?? {};
  const [vals, update] = useDraft<Record<string, unknown>>({}, `param:${sec}`, spec.title, (v) => api.settings.params.put(sec, v));
  const set = (k: string, v: unknown) => update({ ...vals, [k]: v });
  return (
    <Section title={spec.title} help={spec.help}>
      <div className="field-grid">
        {spec.fields.map((f) => {
          const current = f.key in vals ? vals[f.key] : f.key in fileVals ? fileVals[f.key] : P.effective[sec]?.[f.key];
          const eff = P.effective[sec]?.[f.key];
          const fileHas = f.key in fileVals;
          const note = [`settings.yaml: ${fileHas ? show(fileVals[f.key]) : '(없음)'}`, `기본값: ${show(P.defaults[sec]?.[f.key])}`];
          if (fileHas && JSON.stringify(eff) !== JSON.stringify(fileVals[f.key])) note.push(`지금 적용: ${show(eff)} (모델 목록 설정 등)`);
          const help = [f.help, note.join(' · ')].filter(Boolean).join(' — ');
          let input;
          if (f.type === 'bool') {
            input = <span className="set-check"><input type="checkbox" checked={!!current} onChange={(e) => set(f.key, e.target.checked)} /><span className="small">켬</span></span>;
          } else if (f.type === 'select') {
            input = (
              <select className="select" value={String(current ?? '')} onChange={(e) => set(f.key, e.target.value)}>
                {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            );
          } else {
            const isNum = f.type === 'int' || f.type === 'float';
            input = (
              <input type={isNum ? 'number' : 'text'} step={f.step ?? (f.type === 'int' ? 1 : 'any')} min={f.min ?? undefined} max={f.max ?? undefined}
                placeholder={f.nullable ? '비우면 서버 기본값' : ''}
                value={current == null ? '' : Array.isArray(current) ? current.join(', ') : String(current)}
                onChange={(e) => set(f.key, e.target.value === '' ? null : isNum ? Number(e.target.value) : e.target.value)} />
            );
          }
          return <Field key={f.key} label={f.label} help={help}>{input}</Field>;
        })}
      </div>
    </Section>
  );
}
