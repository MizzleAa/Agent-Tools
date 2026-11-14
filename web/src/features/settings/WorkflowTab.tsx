// 워크플로 하나: 설명 · 흐름도 · 노드(이름·id·하네스·용도·기다릴 노드·지시문·최대 스텝·실패 시) + 복제·기본값으로·삭제
// 노드 = 하네스 + 용도: 하네스를 고르면 용도 목록에 그 하네스의 용도가 더해진다 (공통 용도는 늘 있다)
import { useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { useApp } from '@/state/AppState';
import { Editor, Field, Section, SizeLine } from '@/ui/form';
import { Icon } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';
import { FlowDiagram } from './FlowDiagram';
import { fixAfter, renameNode } from './workflowEdit';
import { useDraft, usePending } from './pending';
import { StateBadges, useSettings, VarChips } from './parts';

type Workflow = Schemas['Workflow-Output'];
type WorkflowIn = Schemas['Workflow-Input'];
type Node = Workflow['nodes'][number];
const ON_FAIL: [string, string][] = [['stop', '실패하면 워크플로를 멈춤'], ['continue', '실패해도 이 노드를 기다리지 않는 노드는 계속']];

export function WorkflowTab({ entry }: { entry: Schemas['WorkflowEntry'] }) {
  const { data, reload } = useSettings();
  const ui = useUi();
  const navigate = useNavigate();
  const { reloadCatalog } = useApp();
  const { clear } = usePending();
  const name = entry.name;
  const [draft, update] = useDraft<Workflow>(structuredClone(entry.workflow!), `workflow:${name}`, `워크플로 ${name}`, async (v) => {
    const r = await api.settings.workflows.update(v);
    if (r.warnings?.length) ui.toast(`저장했습니다. 확인할 것:\n${r.warnings.join('\n')}`, 'info', 10000);
  });
  const nodes = draft.nodes;
  const setNodes = (next: Node[]) => update({ ...draft, nodes: next });
  const setNode = (i: number, patch: Partial<Node>) => setNodes(nodes.map((n, k) => (k === i ? { ...n, ...patch } : n)));
  const profiles = data.profiles.filter((p) => p.profile).map((p) => p.name);
  const harnesses = data.harnesses;
  const after = async () => { clear(); await reload(); await reloadCatalog(); };

  const rename = (i: number, neu: string) => {   // 이름을 바꾸면 다른 노드의 연결·{{참조}} 도 같이 바꾼다
    const old = nodes[i].id;
    if (!neu || neu === old) return;
    if (nodes.some((n) => n.id === neu)) { ui.toast(`'${neu}' 노드가 이미 있습니다.`, 'error'); return; }
    setNodes(renameNode(nodes, i, neu));
  };
  const move = (i: number, k: number) => {
    const next = structuredClone(nodes);
    [next[i], next[i + k]] = [next[i + k], next[i]];
    const dropped = fixAfter(next);
    if (dropped.length) ui.toast(`순서가 바뀌어 ${dropped.join(', ')} 의 '기다릴 노드' 연결 일부를 뺐습니다.`);
    setNodes(next);
  };
  const remove = (i: number) => {
    if (nodes.length === 1) { ui.toast('노드가 하나 이상 있어야 합니다.', 'error'); return; }
    const gone = nodes[i].id;
    setNodes(nodes.filter((_, k) => k !== i).map((n) => ({ ...n, after: (n.after ?? []).filter((a) => a !== gone) })));
  };
  const add = () => {
    const last = nodes[nodes.length - 1];
    let k = nodes.length + 1;
    while (nodes.some((n) => n.id === `step${k}`)) k++;
    setNodes([...nodes, { id: `step${k}`, title: `${nodes.length + 1}단계`, harness: last?.harness ?? '', profile: last?.profile ?? 'general',
      prompt: last ? `{{${last.id}}}\n` : '{{input}}\n', after: last ? [last.id] : [], on_fail: 'stop', max_steps: null }]);
  };
  const reset = async () => {
    if (!(await ui.confirm({ title: `'${name}' 를 기본값으로 되돌릴까요?`, message: "지금 내용은 '변경 기록'에 남습니다.", confirmText: '되돌리기', danger: true }))) return;
    try { await api.settings.workflows.reset(name); await after(); } catch (e) { ui.error(e); }
  };
  const del = async () => {
    if (!(await ui.confirm({ title: `'${name}' 워크플로를 지울까요?`, message: "'변경 기록'에서 되살릴 수 있습니다.", confirmText: '지우기', danger: true }))) return;
    try { await api.settings.workflows.remove(name); await after(); navigate('/settings'); } catch (e) { ui.error(e); }
  };

  return (
    <div className="set-stack">
      <Section title={<span className="title set-title">⛓ {name}</span>} right={<>
        <StateBadges modified={entry.modified} />
        <span className="chip">{entry.builtin ? '기본 제공' : '직접 만듦'}</span>
        <button type="button" className="btn" onClick={() => newWorkflow(ui, data, after, navigate, draft)}>복제</button>
        {entry.builtin && entry.modified && <button type="button" className="btn" onClick={reset}>기본값으로</button>}
        <button type="button" className="btn danger" onClick={del}>삭제</button>
      </>} help="입력창의 용도 선택에서 ⛓ 이름을 고르고 요청을 보내면 노드를 차례로 실행합니다. 노드마다 새 컨텍스트로 시작하고, 앞 노드의 결과는 {{노드id}} 로 받습니다. 모델 서버가 하나라 노드는 하나씩 돌고, 같은 대화 폴더를 같이 씁니다.">
        <div className="form-row">
          <input type="text" className="grow" placeholder="이 워크플로가 하는 일 (입력창 선택에 보입니다)" value={draft.description ?? ''}
            onChange={(e) => update({ ...draft, description: e.target.value })} />
        </div>
        {(entry.warnings ?? []).length > 0 && (
          <div className="pv-warn" style={{ marginTop: 10 }}>
            {(entry.warnings ?? []).map((t, i) => <div key={i} className="pv-w warn"><span className="pv-w-icon">⚠</span><span>{t}</span></div>)}
          </div>
        )}
        <FlowDiagram nodes={nodes} />
      </Section>
      {nodes.map((n, i) => <NodeCard key={`${i}-${n.id}`} node={n} index={i} nodes={nodes} profiles={profiles} harnesses={harnesses}
        onChange={(patch) => setNode(i, patch)} onRename={(v) => rename(i, v)} onMove={(k) => move(i, k)} onRemove={() => remove(i)} />)}
      {nodes.length < data.max_nodes && <button type="button" className="btn" onClick={add}>＋ 노드 추가 ({nodes.length}/{data.max_nodes})</button>}
    </div>
  );
}

function NodeCard({ node: n, index: i, nodes, profiles, harnesses, onChange, onRename, onMove, onRemove }: {
  node: Node; index: number; nodes: Node[]; profiles: string[]; harnesses: Schemas['HarnessInfo'][];
  onChange: (p: Partial<Node>) => void; onRename: (id: string) => void; onMove: (k: number) => void; onRemove: () => void;
}) {
  const ta = useRef<HTMLTextAreaElement>(null);
  const earlier = nodes.slice(0, i);
  const h = harnesses.find((x) => x.id === n.harness);
  const own = h?.roles ?? [];                                  // 고른 하네스의 용도 (같은 이름이면 공통 것 대신 쓰인다)
  const shared = profiles.filter((p) => !own.includes(p));
  const missing = n.profile && !own.includes(n.profile) && !profiles.includes(n.profile);
  const pickHarness = (id: string) => {
    const roles = harnesses.find((x) => x.id === id)?.roles ?? [];
    onChange({ harness: id, ...(roles.includes(n.profile) || profiles.includes(n.profile) ? {} : { profile: roles[0] ?? profiles[0] ?? 'general' }) });
  };
  const vars: [string, string][] = [['input', '사용자의 요청'], ['previous', '같은 대화의 이전 답 (이어서 할 때)'],
    ...earlier.map((e): [string, string] => [e.id, `'${e.title || e.id}' 노드의 결과`])];
  return (
    <Section title={<><span className="brow-n">{i + 1}</span><b>{n.title || n.id}</b>
      {n.harness && <span className="chip" title="하네스">{h ? `${h.kind === 'company' ? '🏢' : '📂'} ${h.title}` : `없는 하네스: ${n.harness}`}</span>}
      <span className="chip">{n.profile}</span></>} right={<>
      <button type="button" className="icon-btn" title="위로" disabled={i === 0} onClick={() => onMove(-1)}>↑</button>
      <button type="button" className="icon-btn" title="아래로" disabled={i === nodes.length - 1} onClick={() => onMove(1)}>↓</button>
      <button type="button" className="icon-btn" title="노드 지우기" onClick={onRemove}><Icon name="x" size={14} /></button>
    </>}>
      <div className="field-grid">
        <Field label="노드 이름"><input type="text" placeholder="노드 이름 (예: 조사)" value={n.title ?? ''} onChange={(e) => onChange({ title: e.target.value })} /></Field>
        <Field label="노드 id" help="영어 소문자·숫자·_ — {{이 이름}} 으로 뒤 노드가 결과를 받습니다">
          <input type="text" className="mono" defaultValue={n.id} onBlur={(e) => { onRename(e.target.value.trim()); e.target.value = n.id; }} />
        </Field>
        <Field label="하네스" help="이 노드에 붙일 지침 · 용도 · 자료 묶음. 공통 하네스는 늘 붙고, 고르면 그 하네스가 더해진다">
          <select className="select" value={n.harness ?? ''} onChange={(e) => pickHarness(e.target.value)}>
            <option value="">공통 (입력창에서 고른 하네스)</option>
            {harnesses.map((x) => <option key={x.id} value={x.id}>{x.kind === 'company' ? '🏢' : '📂'} {x.title}</option>)}
            {n.harness && !h && <option value={n.harness}>없는 하네스: {n.harness}</option>}
          </select>
        </Field>
        <Field label="용도" help="이 노드를 실행할 용도(도구·프롬프트)">
          <select className="select" value={n.profile} onChange={(e) => onChange({ profile: e.target.value })}>
            {own.length > 0 && <optgroup label={`${h?.title} 하네스`}>{own.map((p) => <option key={`h-${p}`} value={p}>{p}</option>)}</optgroup>}
            <optgroup label="공통">{shared.map((p) => <option key={p} value={p}>{p}</option>)}</optgroup>
            {missing && <option value={n.profile}>없는 용도: {n.profile}</option>}
          </select>
        </Field>
      </div>
      <Field label="기다릴 노드" help="체크한 노드가 끝나야 실행합니다. 그 노드가 실패하면 이 노드는 건너뜁니다">
        {earlier.length ? (
          <div className="after-list">
            {earlier.map((e) => (
              <label key={e.id} className="set-check">
                <input type="checkbox" checked={(n.after ?? []).includes(e.id)}
                  onChange={(ev) => onChange({ after: ev.target.checked ? [...(n.after ?? []), e.id] : (n.after ?? []).filter((a) => a !== e.id) })} />
                <span className="small">{e.title || e.id}</span>
              </label>
            ))}
          </div>
        ) : <span className="muted small">첫 노드 — 사용자의 요청({'{{input}}'})으로 시작합니다</span>}
      </Field>
      <div className="set-label" style={{ marginTop: 12 }}>지시문</div>
      <Editor inputRef={ta} value={n.prompt} onChange={(v) => onChange({ prompt: v })} rows={5} max={520} />
      <div className="set-foot"><SizeLine text={n.prompt} /></div>
      <VarChips target={ta} onChange={(v) => onChange({ prompt: v })} extra={vars} />
      <div className="field-grid">
        <Field label="최대 스텝" help="비우면 입력창에서 고른 값">
          <input type="number" min={0} step={1} placeholder="입력창 값" value={n.max_steps ?? ''}
            onChange={(e) => onChange({ max_steps: e.target.value === '' ? null : Number(e.target.value) })} />
        </Field>
        <Field label="실패하면">
          <select className="select" value={n.on_fail ?? 'stop'} onChange={(e) => onChange({ on_fail: e.target.value as Node['on_fail'] })}>
            {ON_FAIL.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </Field>
      </div>
    </Section>
  );
}

export async function newWorkflow(ui: ReturnType<typeof useUi>, data: ReturnType<typeof useSettings>['data'],
  after: () => Promise<void>, navigate: (to: string) => void, base?: Workflow) {
  const from: [string, string][] = [['', '빈 워크플로 (노드 1개)'],
    ...data.workflows.filter((w) => w.workflow).map((w): [string, string] => [w.name, `${w.name} 복제`])];
  const r = await ui.form({
    title: base ? `'${base.name}' 복제` : '새 워크플로',
    message: '노드(하네스 + 용도 + 지시문)를 이어 붙여 정해진 순서로 실행합니다. 예: 조사 → 보고서 작성, 만들기 → 검토 → 고치기.',
    fields: [
      { name: 'name', label: '이름 (영어 소문자·숫자·-·_)', placeholder: 'weekly-report', value: base ? `${base.name}-copy` : '' },
      { name: 'description', label: '설명', placeholder: '주간 데이터 정리 → 보고서', value: base?.description ?? '' },
      ...(base ? [] : [{ name: 'from', label: '시작할 내용', options: from }]),
    ],
    confirmText: '만들기',
  });
  if (!r?.name) return;
  const src = base ?? data.workflows.find((w) => w.name === r.from)?.workflow;
  const workflow: WorkflowIn = src ? { ...structuredClone(src), name: r.name, description: r.description }
    : { name: r.name, description: r.description, nodes: [{ id: 'step1', title: '1단계', harness: '', profile: 'general', prompt: '{{input}}\n', after: [] }] };
  try { await api.settings.workflows.create(workflow); } catch (e) { ui.error(e); return; }
  ui.toast(`'${r.name}' 워크플로를 만들었습니다.`, 'ok');
  await after();
  navigate(`/settings/w/${r.name}`);
}
