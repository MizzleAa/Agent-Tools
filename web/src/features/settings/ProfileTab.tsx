// 용도(프로필) 하나: 역할 프롬프트 · 프롬프트 구성(블록 순서) · 도구 · 파라미터 · 변수 + 미리보기·복제·기본값으로·삭제
import { useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { PERM, PERM_ORDER, TOOL_LABEL } from '@/lib/labels';
import { useApp } from '@/state/AppState';
import { Editor, Field, Section, SizeLine, Toggle } from '@/ui/form';
import { Icon } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';
import { useDraft, usePending } from './pending';
import { StateBadges, useSettings, VarChips } from './parts';
import { PreviewBody } from './PreviewSheet';

type Profile = Schemas['Profile-Output'];
type ProfileIn = Schemas['Profile-Input'];
const THINK: [string, string][] = [['', '모델 기본값'], ['true', '켬 (생각한 뒤 답)'], ['false', '끔 (바로 답)']];

export function ProfileTab({ entry }: { entry: Schemas['ProfileEntry'] }) {
  const { data, reload } = useSettings();
  const ui = useUi();
  const navigate = useNavigate();
  const { reloadCatalog } = useApp();
  const { clear } = usePending();
  const role = useRef<HTMLTextAreaElement>(null);
  const name = entry.name;
  const [draft, update] = useDraft<Profile>(structuredClone(entry.profile!), `profile:${name}`, `${name} 프로필`,
    (v) => api.settings.profiles.update(v));
  const set = <K extends keyof Profile>(k: K, v: Profile[K]) => update({ ...draft, [k]: v });
  const blocks = draft.blocks ?? [];

  const preview = async () => {
    try { const r = await api.settings.preview(draft); ui.sheet(`미리보기 — ${name}`, <PreviewBody r={r} />); } catch (e) { ui.error(e); }
  };
  const after = async () => { clear(); await reload(); await reloadCatalog(); };
  const reset = async () => {
    if (!(await ui.confirm({ title: `'${name}' 를 기본값으로 되돌릴까요?`, message: "역할 프롬프트·구성·도구·파라미터가 처음 상태로 돌아갑니다. 지금 내용은 '변경 기록'에 남습니다.", confirmText: '되돌리기', danger: true }))) return;
    try { await api.settings.profiles.reset(name); await after(); } catch (e) { ui.error(e); }
  };
  const remove = async () => {
    if (!(await ui.confirm({ title: `'${name}' 용도를 지울까요?`, message: "지운 프로필은 '변경 기록'에서 되살릴 수 있습니다.", confirmText: '지우기', danger: true }))) return;
    try { await api.settings.profiles.remove(name); await after(); navigate('/settings'); } catch (e) { ui.error(e); }
  };

  // 블록 구성
  const byId = Object.fromEntries(data.blocks.map((b) => [b.id, b]));
  const move = (i: number, k: number) => { const b = [...blocks]; [b[i], b[i + k]] = [b[i + k], b[i]]; set('blocks', b); };
  const rest = data.blocks.filter((b) => !blocks.includes(b.id));
  // 도구
  const tools = [...data.tools].sort((a, b) => (PERM_ORDER[a.permission] - PERM_ORDER[b.permission]) || a.name.localeCompare(b.name));
  const full = draft.tools.length >= data.max_tools;
  const eff = data.params.effective;
  const num = (v: string) => (v === '' ? null : Number(v));
  const vars = Object.entries(draft.variables ?? {});
  const addVar = async () => {
    const r = await ui.form({ title: '이 용도 전용 변수', message: '같은 이름의 전역 변수보다 우선합니다. 프롬프트에 {{이름}} 으로 씁니다.',
      fields: [{ name: 'name', label: '이름 (영어 소문자·숫자·_)', placeholder: 'tone' }, { name: 'value', label: '값', placeholder: '존댓말로 짧게' }] });
    if (r?.name) set('variables', { ...(draft.variables ?? {}), [r.name]: r.value });
  };

  return (
    <div className="set-stack">
      <Section title={<span className="title set-title">{name}</span>} right={<>
        <StateBadges modified={entry.modified} />
        <span className="chip">{entry.builtin ? '기본 제공' : '직접 만듦'}</span>
        <button type="button" className="btn primary" onClick={preview}>미리보기</button>
        <button type="button" className="btn" onClick={() => newProfile(ui, data, after, navigate, draft)}>복제</button>
        {entry.builtin && entry.modified && <button type="button" className="btn" onClick={reset}>기본값으로</button>}
        {!entry.builtin && <button type="button" className="btn danger" onClick={remove}>삭제</button>}
      </>}>
        <div className="form-row">
          <input type="text" className="grow" placeholder="이 용도 설명 (입력창의 프로필 선택에 보입니다)" value={draft.description ?? ''}
            onChange={(e) => set('description', e.target.value)} />
        </div>
        <div className="muted small" style={{ marginTop: 6 }}>파일: {entry.file}</div>
      </Section>

      <Section title="역할 프롬프트" help="시스템 프롬프트 맨 앞에 들어갑니다. 이 용도의 정체성·목표·일하는 순서·하지 말 것을 씁니다. 공통 규칙은 아래 블록에 있으니 되풀이하지 않습니다.">
        <Editor inputRef={role} value={draft.system_prompt} onChange={(v) => set('system_prompt', v)} rows={8} />
        <div className="set-foot"><SizeLine text={draft.system_prompt} /></div>
        <VarChips target={role} onChange={(v) => set('system_prompt', v)} />
      </Section>

      <Section title="프롬프트 구성" help="역할 프롬프트 뒤에 이 순서대로 붙습니다. 잘 안 바뀌는 블록을 앞에, 대화마다 바뀌는 '실행 환경'과 실행 중에 붙는 '언어별 규격'을 뒤에 두면 새 대화의 첫 응답이 빨라집니다.">
        <div className="brow fixed"><span className="brow-n">1</span>
          <div className="brow-main"><div className="brow-title"><b>역할 프롬프트</b><span className="chip">맨 앞 고정</span></div></div></div>
        {blocks.map((id, i) => {
          const b = byId[id];
          return (
            <div key={id} className={`brow${b ? '' : ' missing'}`}>
              <span className="brow-n">{i + 2}</span>
              <div className="brow-main">
                <div className="brow-title"><b>{b ? b.title : id}</b><span className="chip">{id}</span>
                  {b?.dynamic && <span className="badge">자동</span>}{b?.modified && <span className="badge warn">수정됨</span>}
                  {!b && <span className="badge fail">없는 블록</span>}</div>
                {b && <div className="muted small">{b.description}</div>}
              </div>
              <div className="brow-actions">
                {b && !b.dynamic && <button type="button" className="mini-btn" onClick={() => navigate(`/settings/blocks/${id}`)}>편집</button>}
                <button type="button" className="icon-btn" title="위로" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                <button type="button" className="icon-btn" title="아래로" disabled={i === blocks.length - 1} onClick={() => move(i, 1)}>↓</button>
                <button type="button" className="icon-btn" title="빼기" onClick={() => set('blocks', blocks.filter((_, k) => k !== i))}><Icon name="x" size={14} /></button>
              </div>
            </div>
          );
        })}
        <div className="form-row">
          <select className="select" value="" onChange={(e) => { if (e.target.value) set('blocks', [...blocks, e.target.value]); }}>
            <option value="">＋ 블록 추가…</option>
            {rest.map((b) => <option key={b.id} value={b.id}>{b.title} ({b.id})</option>)}
          </select>
        </div>
      </Section>

      <Section title={<><span className="title">도구</span><span className={`chip${full ? ' warn' : ''}`}>{draft.tools.length} / {data.max_tools}</span></>}
        help="이 용도에서 쓸 수 있는 도구. 소형 모델은 도구가 적을수록 정확합니다. 읽기는 자동, 쓰기·실행·API 변경은 승인을 받습니다. 설명은 '도구 설명' 탭에서 고칩니다.">
        <div className="tool-grid">
          {tools.map((t) => {
            const on = draft.tools.includes(t.name);
            return (
              <label key={t.name} className={`tool-opt${on ? ' on' : ''}`} title={t.text}>
                <input type="checkbox" checked={on} disabled={!on && full}
                  onChange={(e) => set('tools', e.target.checked ? [...draft.tools, t.name] : draft.tools.filter((x) => x !== t.name))} />
                <span className="tool-opt-name">{TOOL_LABEL[t.name] ?? t.name}<span className="muted small">{t.name}</span></span>
                <span className={`badge ${t.permission}`}>{PERM[t.permission]}</span>
              </label>
            );
          })}
        </div>
      </Section>

      <Section title="파라미터" help="비워 두면 '실행 파라미터' 탭의 전역값을 씁니다. 입력창에서 고른 스텝이 있으면 그게 우선합니다.">
        <div className="field-grid">
          <Field label="최대 스텝" help="0 = 무제한">
            <input type="number" step={1} placeholder={`전역값 ${eff.limits.max_steps}`} value={draft.max_steps ?? ''} onChange={(e) => set('max_steps', num(e.target.value))} />
          </Field>
          <Field label="생각 모드" help="켜면 정확해지지만 느려집니다. 도구 호출이 많은 작업은 끔 권장">
            <select className="select" value={draft.think == null ? '' : String(draft.think)}
              onChange={(e) => set('think', e.target.value === '' ? null : e.target.value === 'true')}>
              {THINK.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </Field>
          <Field label="온도" help="조사·사내 조회는 낮게(0.3~0.6), 글쓰기는 높게">
            <input type="number" step={0.05} placeholder={`전역값 ${eff.sampling.temperature}`} value={draft.temperature ?? ''} onChange={(e) => set('temperature', num(e.target.value))} />
          </Field>
          <Field label="top_p">
            <input type="number" step={0.05} placeholder={`전역값 ${eff.sampling.top_p}`} value={draft.top_p ?? ''} onChange={(e) => set('top_p', num(e.target.value))} />
          </Field>
        </div>
        <Toggle checked={!!draft.auto_approve_writes} onChange={(v) => set('auto_approve_writes', v)} label="쓰기 자동 승인" help=" — 허용된 쓰기 폴더 안의 파일 저장은 묻지 않습니다" />
        <Toggle checked={!!draft.standards} onChange={(v) => set('standards', v)} label="개발 규격 적용" help=" — 언어별 규격 붙이기 + 끝날 때 산출물·보고 점검 (개발용)" />
      </Section>

      <Section title="변수 (이 용도에서만)" help={`전역 변수: ${data.variables.map((v) => `{{${v.name}}} = ${v.value}`).join(' · ') || '없음'} — 같은 이름을 여기 만들면 이 용도에서만 값이 바뀝니다.`}>
        {vars.length === 0 && <div className="muted small" style={{ marginTop: 8 }}>이 용도 전용 변수가 없습니다.</div>}
        {vars.map(([k, v]) => (
          <div key={k} className="form-row">
            <span className="chip mono">{`{{${k}}}`}</span>
            <input type="text" className="grow" value={v} onChange={(e) => set('variables', { ...draft.variables, [k]: e.target.value })} />
            <button type="button" className="icon-btn" title="지우기" onClick={() => { const n = { ...draft.variables }; delete n[k]; set('variables', n); }}><Icon name="x" size={14} /></button>
          </div>
        ))}
        <div className="form-row"><button type="button" className="btn" onClick={addVar}>＋ 변수 추가</button></div>
      </Section>
    </div>
  );
}

/** 새 용도 (빈 것 또는 base 복제) */
export async function newProfile(ui: ReturnType<typeof useUi>, data: ReturnType<typeof useSettings>['data'],
  after: () => Promise<void>, navigate: (to: string) => void, base?: Profile) {
  const from: [string, string][] = [['', '빈 프로필 (기본 블록 · 파일 읽기·쓰기 도구)'],
    ...data.profiles.filter((p) => p.profile).map((p): [string, string] => [p.name, `${p.name} 복제`])];
  const r = await ui.form({
    title: base ? `'${base.name}' 복제` : '새 용도 만들기',
    message: '용도마다 프롬프트·도구·파라미터를 따로 둡니다. 이름은 입력창의 프로필 선택에 보입니다.',
    fields: [
      { name: 'name', label: '이름 (영어 소문자·숫자·-·_)', placeholder: 'report-writer', value: base ? `${base.name}-copy` : '' },
      { name: 'description', label: '설명', placeholder: '주간 보고서를 정리해 파일로 저장', value: base?.description ?? '' },
      ...(base ? [] : [{ name: 'from', label: '시작할 내용', options: from }]),
    ],
    confirmText: '만들기',
  });
  if (!r?.name) return;
  const src = base ?? data.profiles.find((p) => p.name === r.from)?.profile;
  const profile: ProfileIn = src ? { ...structuredClone(src), name: r.name, description: r.description } : {
    name: r.name, description: r.description,
    system_prompt: '너는 (무엇을 하는) 담당이다.\n1. (먼저 무엇을 확인한다)\n2. (그다음 무엇을 한다)\n3. (결과를 어떤 형식으로 답한다)\n',
    blocks: ['work_rules', 'environment'], tools: ['list_dir', 'read_file', 'search_files', 'write_file'],
    think: false, auto_approve_writes: false, standards: false, variables: {},
  };
  try { await api.settings.profiles.create(profile); } catch (e) { ui.error(e); return; }
  ui.toast(`'${r.name}' 용도를 만들었습니다.`, 'ok');
  await after();
  navigate(`/settings/p/${r.name}`);
}
