// 공통 탭: 블록 · 루프 메시지 · 도구 설명 · 변수 (모두 config/prompts.yaml 에 바뀐 것만 저장)
import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { PERM, PERM_ORDER, TOOL_LABEL } from '@/lib/labels';
import { Editor, Field, Section, SizeLine } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { useDraft, usePending } from './pending';
import { StateBadges, useSettings, VarChips } from './parts';

function useResetter() {
  const ui = useUi();
  const { reload } = useSettings();
  const { clear } = usePending();
  return async (title: string, remove: boolean, fn: () => Promise<unknown>) => {
    if (!(await ui.confirm({ title: remove ? `'${title}' 를 지울까요?` : `'${title}' 를 기본값으로 되돌릴까요?`,
      message: "지금 내용은 '변경 기록'에 남습니다.", confirmText: remove ? '지우기' : '되돌리기', danger: true }))) return;
    try { await fn(); clear(); await reload(); } catch (e) { ui.error(e); }
  };
}

// ───────────────────────── 블록

export function BlocksTab({ selected }: { selected?: string }) {
  const { data, reload } = useSettings();
  const ui = useUi();
  const navigate = useNavigate();
  const sel = data.blocks.find((b) => b.id === selected) ?? data.blocks[0];
  const group = (title: string, items: Schemas['BlockInfo'][]) => items.length > 0 && (
    <>
      <div className="list-group">{title}</div>
      {items.map((b) => (
        <button key={b.id} type="button" className={`list-item${b.id === sel.id ? ' active' : ''}`} onClick={() => navigate(`/settings/blocks/${b.id}`)}>
          <span className="list-title">{b.title}</span>
          <span className="muted small">{b.id}{b.used_by.length ? ` · ${b.used_by.join(', ')}` : ' · 안 쓰임'}</span>
          {b.modified && <span className="stab-dot" title="수정됨" />}
        </button>
      ))}
    </>
  );
  const create = async () => {
    const r = await ui.form({ title: '새 블록', message: "여러 용도에서 같이 쓸 프롬프트 조각입니다. 만든 뒤 각 용도의 '프롬프트 구성'에 넣습니다.",
      fields: [{ name: 'id', label: '이름 (영어 소문자·숫자·_)', placeholder: 'report_format' }, { name: 'title', label: '제목', placeholder: '보고서 형식' }],
      confirmText: '만들기' });
    if (!r?.id) return;
    try { await api.settings.blocks.put(r.id, { title: r.title || r.id, description: '', text: `## ${r.title || r.id}\n- \n` }); }
    catch (e) { ui.error(e); return; }
    await reload();
    navigate(`/settings/blocks/${r.id}`);
  };
  return (
    <div className="split">
      <div className="list">
        <button type="button" className="btn" style={{ width: '100%', marginBottom: 8 }} onClick={create}>＋ 새 블록</button>
        {group('기본 제공', data.blocks.filter((b) => b.default != null))}
        {group('직접 만든 블록', data.blocks.filter((b) => b.custom))}
        {group('자동 (프로그램이 채움)', data.blocks.filter((b) => b.dynamic))}
      </div>
      {sel.dynamic ? (
        <Section title={sel.title}>
          <div className="muted" style={{ marginTop: 8 }}>{sel.description}</div>
          <div className="muted small" style={{ marginTop: 8 }}>내용은 실행할 때 프로그램이 채웁니다. 어느 용도에 넣을지·순서는 각 용도 탭의 '프롬프트 구성'에서 정합니다.</div>
          <div className="form-row"><span className="muted small">쓰는 용도:</span>{sel.used_by.map((n) => <span key={n} className="chip">{n}</span>)}</div>
        </Section>
      ) : <BlockEditor key={sel.id} block={sel} />}
    </div>
  );
}

function BlockEditor({ block }: { block: Schemas['BlockInfo'] }) {
  const ui = useUi();
  const resetter = useResetter();
  const ta = useRef<HTMLTextAreaElement>(null);
  const [draft, update] = useDraft<Schemas['BlockWrite']>({ title: block.title, description: block.description, text: block.text },
    `block:${block.id}`, `블록 ${block.id}`, (v) => api.settings.blocks.put(block.id, v));
  return (
    <Section title={<><span className="title set-title">{block.title}</span><span className="chip">{block.id}</span><StateBadges modified={block.modified} custom={block.custom} /></>}
      right={<>
        {block.modified && <button type="button" className="btn" onClick={() => ui.sheet(`기본값 — ${block.title}`, <pre className="block">{block.default}</pre>)}>기본값 보기</button>}
        {block.modified && <button type="button" className="btn" onClick={() => resetter(block.title, false, () => api.settings.blocks.reset(block.id))}>기본값으로</button>}
        {block.custom && <button type="button" className="btn danger" onClick={() => resetter(block.title, true, () => api.settings.blocks.reset(block.id))}>삭제</button>}
      </>}>
      <div className="field-grid">
        <Field label="제목"><input type="text" value={draft.title ?? ''} onChange={(e) => update({ ...draft, title: e.target.value })} /></Field>
        <Field label="설명"><input type="text" placeholder="언제 쓰는 블록인지 (화면에만 보임)" value={draft.description ?? ''} onChange={(e) => update({ ...draft, description: e.target.value })} /></Field>
      </div>
      <Editor inputRef={ta} value={draft.text} onChange={(v) => update({ ...draft, text: v })} rows={14} max={900} />
      <div className="set-foot"><SizeLine text={draft.text} />
        <span className="muted small">{block.used_by.length ? `쓰는 용도: ${block.used_by.join(', ')}` : '아직 어느 용도에도 넣지 않았습니다'}</span></div>
      <VarChips target={ta} onChange={(v) => update({ ...draft, text: v })} />
      <div className="muted small set-help">줄 맨 앞에 {'{{?도구이름}}'} 을 쓰면 그 도구를 가진 용도에만 그 줄이 들어갑니다 (예: {'{{?run_command}}'}- 명령은 …). {'{{?!이름}}'} 은 반대.</div>
    </Section>
  );
}

// ───────────────────────── 루프 메시지

export function MessagesTab() {
  const { data } = useSettings();
  return (
    <div className="set-stack">
      <div className="muted set-intro">에이전트 루프가 모델에게 끼어들 때 하는 말입니다 (계획만 말할 때 되돌려 보내기, 한도·반복·실패 경고, 규격 점검 …). {'{{변수}}'} 는 그때의 값으로 바뀝니다. 짧고 구체적으로, 무엇을 하라는지 한 가지씩 쓰는 게 소형 모델에 잘 듣습니다.</div>
      {data.messages.map((m) => <MessageCard key={m.key} m={m} />)}
    </div>
  );
}

function MessageCard({ m }: { m: Schemas['MessageInfo'] }) {
  const resetter = useResetter();
  const ta = useRef<HTMLTextAreaElement>(null);
  const [text, update] = useDraft(m.text, `msg:${m.key}`, `메시지 ${m.key}`, (v) => api.settings.messages.put(m.key, v));
  return (
    <Section title={<><span className="title">{m.title}</span><span className="chip">{m.key}</span><StateBadges modified={m.modified} /></>}
      right={m.modified && <button type="button" className="mini-btn" onClick={() => resetter(m.title, false, () => api.settings.messages.reset(m.key))}>기본값으로</button>}
      help={m.when}>
      <Editor inputRef={ta} value={text} onChange={update} rows={2} max={360} />
      {Object.keys(m.vars).length > 0 && <VarChips target={ta} onChange={update} extra={Object.entries(m.vars)} />}
    </Section>
  );
}

// ───────────────────────── 도구 설명

export function ToolsTab() {
  const { data } = useSettings();
  const tools = [...data.tools].sort((a, b) => (PERM_ORDER[a.permission] - PERM_ORDER[b.permission]) || a.name.localeCompare(b.name));
  return (
    <div className="set-stack">
      <div className="muted set-intro">모델이 도구를 고를 때 읽는 설명입니다. 언제 쓰는지·인자를 어떻게 채우는지·쓰면 안 되는 경우를 짧게 씁니다. 비우거나 '기본값으로'를 누르면 코드에 적힌 설명을 씁니다.</div>
      {tools.map((t) => <ToolCard key={t.name} t={t} />)}
    </div>
  );
}

function ToolCard({ t }: { t: Schemas['ToolInfo'] }) {
  const resetter = useResetter();
  const [touched, setTouched] = useState(false);
  const [text, update] = useDraft(t.text, `tool:${t.name}`, `도구 ${t.name}`, (v) => api.settings.tools.put(t.name, v));
  return (
    <details className="card wide set-card tool-card" open={t.modified || touched || undefined}>
      <summary className="card-head">
        <span className="title">{TOOL_LABEL[t.name] ?? t.name}</span><span className="chip">{t.name}</span>
        <span className={`badge ${t.permission}`}>{PERM[t.permission]}</span><StateBadges modified={t.modified} />
        <span className="spacer" /><span className="muted small">{t.used_by.length ? t.used_by.join(', ') : '안 쓰임'}</span>
      </summary>
      <div className="tool-card-body">
        <Editor value={text} onChange={(v) => { setTouched(true); update(v); }} rows={3} max={420} />
        <div className="set-foot"><SizeLine text={text} />
          {t.modified && <button type="button" className="mini-btn" onClick={() => resetter(t.name, false, () => api.settings.tools.reset(t.name))}>기본값으로</button>}</div>
        <details><summary className="muted small">인자 스키마 (코드에서 정해짐)</summary><pre className="block">{JSON.stringify(t.params, null, 2)}</pre></details>
      </div>
    </details>
  );
}

// ───────────────────────── 변수

export function VariablesTab() {
  const { data, reload } = useSettings();
  const ui = useUi();
  const add = async () => {
    const r = await ui.form({ title: '새 전역 변수', message: '모든 용도의 프롬프트에서 {{이름}} 으로 쓸 수 있습니다. 용도 탭에서 같은 이름으로 값을 따로 줄 수 있습니다.',
      fields: [{ name: 'name', label: '이름 (영어 소문자·숫자·_)', placeholder: 'company' }, { name: 'value', label: '값', placeholder: '우리 회사 이름' },
        { name: 'description', label: '설명', placeholder: '보고서 머리에 쓰는 회사 이름' }], confirmText: '만들기' });
    if (!r?.name) return;
    try { await api.settings.variables.put(r.name, { value: r.value, description: r.description }); await reload(); } catch (e) { ui.error(e); }
  };
  return (
    <div className="set-stack">
      <Section title="전역 변수" right={<button type="button" className="btn" onClick={add}>＋ 변수 추가</button>}
        help="블록·역할 프롬프트·메시지·도구 설명 어디서나 {{이름}} 으로 씁니다. 회사 이름, 답변 언어·말투, 부서 규칙처럼 여러 곳에 같은 값을 쓸 때 편합니다.">
        <table className="set-table">
          <thead><tr><th>이름</th><th>값</th><th>설명</th><th /></tr></thead>
          <tbody>{data.variables.map((v) => <VariableRow key={v.name} v={v} />)}</tbody>
        </table>
      </Section>
      <Section title="내장 변수" help="프로그램이 실행할 때 채웁니다. 이름을 바꾸거나 같은 이름의 변수를 만들 수 없습니다.">
        <table className="set-table"><tbody>
          {data.builtin_vars.map((v) => <tr key={v.name}><td><code className="inline">{`{{${v.name}}}`}</code></td><td>{v.description}</td></tr>)}
        </tbody></table>
      </Section>
    </div>
  );
}

function VariableRow({ v }: { v: Schemas['VariableInfo'] }) {
  const resetter = useResetter();
  const [draft, update] = useDraft<Schemas['VariableWrite']>({ value: v.value, description: v.description }, `var:${v.name}`, `변수 ${v.name}`,
    (x) => api.settings.variables.put(v.name, x));
  return (
    <tr>
      <td><code className="inline">{`{{${v.name}}}`}</code> <StateBadges modified={v.modified} custom={v.custom} /></td>
      <td><input type="text" value={draft.value ?? ''} onChange={(e) => update({ ...draft, value: e.target.value })} /></td>
      <td><input type="text" placeholder="설명" value={draft.description ?? ''} onChange={(e) => update({ ...draft, description: e.target.value })} /></td>
      <td>
        {v.custom ? <button type="button" className="mini-btn" onClick={() => resetter(v.name, true, () => api.settings.variables.reset(v.name))}>삭제</button>
          : v.modified ? <button type="button" className="mini-btn" onClick={() => resetter(v.name, false, () => api.settings.variables.reset(v.name))}>기본값으로</button> : null}
      </td>
    </tr>
  );
}
