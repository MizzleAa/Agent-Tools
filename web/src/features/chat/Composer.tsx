// 입력창: 요청 + 모델 · 용도(또는 워크플로) · 승인 방식 · 최대 스텝 + 보내기/중지
// 모델 선택(ModelSelect)은 Claude Code(CLI) 입력창도 같이 쓴다: 로컬(오프라인) · 외부 API · CLI(온라인)
import { type KeyboardEvent, useLayoutEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '@/api';
import { CC_MODELS, ccProblem, type CcEngine, cliChoice, ENGINE_LABEL, isCc, parseCli } from '@/features/claudecode/lib';
import { useCc } from '@/features/claudecode/store';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { Icon } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';
import { BaseDirSelect } from './BaseDirSelect';

const MODEL_STATE: Record<string, string> = { ready: '', downloading: ' (내려받는 중)', missing: ' (파일 없음)' };

export interface ComposerOptions {
  choice: string;              // 용도 이름 또는 wf:<워크플로>
  approve: string;             // none | writes | all
  maxSteps: string;
  effort: string;              // 외부 모델 추론 강도: '' (연결 기본) | low | medium | high | xhigh | max
  base: string;                // 작업 위치: 새 작업 폴더를 만들 상위 폴더 ('' = workspace, 관리자만)
  harness: string;             // 회사·프로젝트 하네스 ('' = 공통만) — 공통 하네스에 더해 붙는다
}

type HarnessChoice = ReturnType<typeof useApp>['harnesses'][number];

/** 용도 목록: 공통 용도 + 늘 붙는 회사 하네스의 용도 + 고른 하네스의 용도 (같은 이름이면 하네스 것이 쓰인다) */
export function roleGroups(profiles: { name: string; description: string }[], harnesses: HarnessChoice[], chosen: string) {
  const extra = harnesses.filter((h) => h.auto || h.id === chosen).filter((h) => h.roles.length);
  const own = new Set(extra.flatMap((h) => h.roles.map((r) => r.name)));
  return { extra, shared: profiles.filter((p) => !own.has(p.name)) };
}

/** 하네스 고르기 — 고를 하네스가 없으면 그리지 않는다 (회사 하네스는 그 회사 사람에게 늘 붙으므로 목록에서 표시만) */
export function HarnessSelect({ value, onChange, disabled }: { value: string; onChange: (v: string) => void; disabled?: boolean }) {
  const { harnesses } = useApp();
  const pickable = harnesses.filter((h) => !h.auto);
  if (!pickable.length && !value) return null;
  const auto = harnesses.filter((h) => h.auto).map((h) => h.title);
  const known = harnesses.some((h) => h.id === value);
  return (
    <select className="ghost-select" value={known ? value : ''} disabled={disabled} onChange={(e) => onChange(e.target.value)}
      title={`하네스 — 공통 하네스(지침 · 용도 · 자료)에 더해 붙일 회사·프로젝트 하네스${auto.length ? `\n늘 붙음: ${auto.join(', ')}` : ''}`}>
      <option value="">하네스: 공통</option>
      {pickable.map((h) => <option key={h.id} value={h.id} title={h.description}>하네스: {h.kind === 'company' ? '🏢' : '📂'} {h.title}</option>)}
    </select>
  );
}

// 추론 강도 — 작업마다 고른다. 외부 모델: 공급사의 추론 강도 (OpenAI·Gemini 는 '높음'까지, 그보다 높은 값은 서버가 '높음'으로).
// 로컬 모델: 낮음 = 생각 끄고 빠르게, 높음 = 생각 켜고 꼼꼼히, 기본 = 용도 설정대로
export const EFFORT_LABELS: [string, string][] = [['', '추론: 기본'], ['low', '추론: 낮음'], ['medium', '추론: 보통'], ['high', '추론: 높음'],
  ['xhigh', '추론: 아주 높음'], ['max', '추론: 최대']];
const LOCAL_EFFORTS: [string, string][] = [['', '추론: 기본'], ['low', '추론: 낮음 (빠르게)'], ['high', '추론: 높음 (생각 켬)']];

export { ccProblem };
export const useCcStatus = () => useCc((s) => s.status);

/** 모델 고르기: 로컬 (이 서버) · 외부 API 연결 · CLI · Claude Code (관리자만). value/onCcModel 은 CLI 대화 안에서 세션 모델을 바꿀 때 */
export function ModelSelect({ running, value, onCcModel }: { running: boolean; value?: string; onCcModel?: (model: string) => void }) {
  const { status, refreshStatus, modelChoice, setModelChoice } = useApp();
  const { isAdmin } = useAuth();
  const ui = useUi();
  const navigate = useNavigate();
  const cc = useCcStatus();
  const models = status?.models ?? [];
  const active = models.find((m) => m.active)?.name ?? '';
  const locals = isAdmin ? models : models.filter((m) => m.active);   // 로컬 모델 바꾸기(모델 서버 다시 띄우기)는 관리자만
  const external = status?.external_models ?? [];
  const groups = [...new Set(external.map((m) => m.connection))].map((c) => external.filter((m) => m.connection === c));
  const selectModel = async (name: string) => {
    if (name === active) return;
    if (running) { ui.toast('실행 중인 작업이 끝난 뒤에 모델을 바꿀 수 있습니다.'); return; }
    if (!(await ui.confirm({ title: `'${name}' 모델로 바꿀까요?`, message: '모델 서버를 다시 띄우느라 수십 초 걸립니다. 그동안은 메시지를 보낼 수 없어요.', confirmText: '바꾸기' }))) return;
    try { await api.system.selectModel(name); } catch (e) { ui.error(e); }
    refreshStatus();
  };
  const current = value ?? (modelChoice || `local:${active}`);
  const onModel = (v: string) => {
    if (v === '__connect__') { navigate('/admin/providers'); return; }   // 관리자: 외부 모델 연결하러
    if (v === '__cli__') { navigate('/admin/providers?view=cli'); return; }   // 관리자: CLI 연결 보러
    const next = parseCli(v), now = parseCli(current);
    if (next && now && next.engine === now.engine && onCcModel) { onCcModel(next.model); setModelChoice(v); return; }   // CLI 대화 안: 그 세션의 모델만 바꾼다
    const crossed = isCc(v) !== isCc(current) || (next && now && next.engine !== now.engine);   // 로컬·API ↔ CLI, CLI 엔진끼리는 새 대화로
    if (crossed && (isCc(current) || value !== undefined || window.location.pathname.startsWith('/c/'))) navigate('/');
    if (v.startsWith('ext:') || isCc(v)) { setModelChoice(v); return; }   // 외부 모델·CLI: 다음 실행부터 그것으로
    setModelChoice('');
    selectModel(v.slice('local:'.length));
  };
  const problemOf = (e: CcEngine) => (isAdmin ? ccProblem(cc, e) : '');
  const engineModels = (e: 'codex' | 'gemini'): [string, string][] => {
    const info = cc?.engines?.find((x) => x.engine === e);
    return [['', '기본 모델'], ...(info?.models ?? []).map((m): [string, string] => [m.id, m.label || m.id])];
  };
  const cliOption = (e: CcEngine, m: string, label: string) => {
    const v = cliChoice(e, m), p = problemOf(e);
    return <option key={v} value={v} disabled={Boolean(p) && current !== v}>⌘ {ENGINE_LABEL[e]} · {label}{p ? ` (${p})` : ''}</option>;
  };
  return (
    <>
      {(models.length > 0 || external.length > 0 || isAdmin) && (
        <select className="ghost-select model-select" value={current} onChange={(e) => onModel(e.target.value)}
          title={'모델 — 로컬은 이 서버의 GPU 에서 (오프라인), ☁ 외부 모델은 회사 계정으로 공급사에 보내 처리합니다 (온라인)' + (isAdmin ? ' · CLI 는 이 PC 에 설치된 Claude Code · Codex · Gemini 로 작업합니다' : '')}>
          {locals.length > 0 && (
            <optgroup label="로컬 (이 서버 · 오프라인)">
              {locals.map((m) => (
                <option key={m.name} value={`local:${m.name}`} disabled={m.state !== 'ready' && !m.active} title={m.note}>{m.name}{MODEL_STATE[m.state]}</option>
              ))}
            </optgroup>
          )}
          {groups.map((items) => (
            <optgroup key={items[0].connection} label={`${items[0].connection_title} (외부 · 온라인)`}>
              {items.map((m) => (
                <option key={m.key} value={m.key} disabled={!m.ready && m.key !== modelChoice}
                  title={m.problem ? `연결 문제: ${m.problem}` : `컨텍스트 ${m.context_length.toLocaleString()} 토큰`}>☁ {m.title}{m.ready ? '' : ' (연결 문제)'}</option>
              ))}
            </optgroup>
          ))}
          {groups.length === 0 && (   // 아직 연결한 외부 모델이 없어도 고를 수 있는 종류가 보이게 (관리자는 바로 연결하러 간다)
            <optgroup label={status?.access.offline ? '외부 모델 (폐쇄망 스위치로 꺼짐)' : '외부 모델 (아직 연결 안 됨)'}>
              {['Claude', 'OpenAI', 'Gemini'].map((n) => <option key={n} value={`none:${n}`} disabled>☁ {n} — 관리자가 연결해야 함</option>)}
              {isAdmin && !status?.access.offline && <option value="__connect__">＋ 외부 모델 연결하기…</option>}
            </optgroup>
          )}
          {isAdmin && (   // CLI: 이 PC 에 설치·로그인된 Claude Code · Codex · Gemini (관리자만 — 이 PC 계정과 파일 권한으로 일한다)
            <optgroup label="CLI (외부 · 온라인)">
              {CC_MODELS.map(([m, label]) => cliOption('claude', m, label))}
              {engineModels('codex').map(([m, label]) => cliOption('codex', m, label))}
              {engineModels('gemini').map(([m, label]) => cliOption('gemini', m, label))}
              <option value="__cli__">＋ CLI 연결 관리…</option>
            </optgroup>
          )}
          {groups.length > 0 && isAdmin && <option value="__connect__">＋ 외부 모델 연결 관리…</option>}
        </select>
      )}
    </>
  );
}

export function Composer({ text, setText, options, setOption, running, onSend, onStop, isNew }: {
  text: string; setText: (v: string) => void;
  options: ComposerOptions; setOption: (key: keyof ComposerOptions, v: string) => void;
  running: boolean; onSend: () => void; onStop: () => void; isNew: boolean;   // isNew: 새 대화 (작업 위치를 고를 수 있음)
}) {
  const { profiles, harnesses, workflows, modelReady, external: chosen } = useApp();
  const { isAdmin } = useAuth();
  const ta = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  const { extra, shared } = roleGroups(profiles, harnesses, options.harness);
  const profile = extra.flatMap((h) => h.roles).find((p) => p.name === options.choice) ?? profiles.find((p) => p.name === options.choice);
  const wf = workflows.find((w) => `wf:${w.name}` === options.choice);
  const choiceTitle = profile ? `${profile.description}\n도구: ${profile.tools.join(', ')}`
    : wf ? `${wf.description}\n노드: ${wf.nodes.map((n) => `${n.title}(${n.profile})`).join(' → ')}` : '';
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {   // Shift+Enter = 줄바꿈
      e.preventDefault();
      if (!running) onSend();
    }
  };
  const allAuto = options.approve === 'all';
  return (
    <>
      <div className="composer">
        <textarea ref={ta} rows={1} placeholder="무엇이든 시켜보세요" value={text} autoFocus
          onChange={(e) => setText(e.target.value)} onKeyDown={onKey} />
        <div className="composer-bar">
          <ModelSelect running={running} />
          {(() => {
            const levels = !chosen ? LOCAL_EFFORTS : EFFORT_LABELS.filter(([v]) => chosen.kind === 'anthropic' || !['xhigh', 'max'].includes(v));
            const value = levels.some(([v]) => v === options.effort) ? options.effort : options.effort ? 'high' : '';
            return (
              <select className="ghost-select" value={value} onChange={(e) => setOption('effort', e.target.value)}
                title={chosen ? '추론 강도 — 높을수록 더 오래 생각해 꼼꼼하지만 느리고 요금이 늘어납니다. 기본 = 관리자가 연결에 정한 값'
                  : '추론 강도 (로컬 모델) — 낮음: 생각을 끄고 빠르게 답함 · 높음: 생각을 켜고 꼼꼼히 · 기본: 용도 설정대로'}>
                {levels.map(([v, label]) => <option key={v || 'default'} value={v}>{label}</option>)}
              </select>
            );
          })()}
          <HarnessSelect value={options.harness} onChange={(v) => setOption('harness', v)} />
          <select className="ghost-select" title={choiceTitle} value={options.choice} onChange={(e) => setOption('choice', e.target.value)}>
            {extra.map((h) => (
              <optgroup key={h.id} label={`용도 · ${h.title}`}>
                {h.roles.map((p) => <option key={`${h.id}/${p.name}`} value={p.name} title={p.description}>{p.name}</option>)}
              </optgroup>
            ))}
            <optgroup label={extra.length ? '용도 · 공통' : '용도'}>{shared.map((p) => <option key={p.name} value={p.name} title={p.description}>{p.name}</option>)}</optgroup>
            {workflows.length > 0 && (
              <optgroup label="워크플로 (노드를 차례로)">
                {workflows.map((w) => <option key={w.name} value={`wf:${w.name}`} title={w.description}>⛓ {w.name}</option>)}
              </optgroup>
            )}
          </select>
          {isAdmin && isNew && <BaseDirSelect value={options.base} onChange={(v) => setOption('base', v)} />}
          <select className={`ghost-select${allAuto ? ' danger' : ''}`} title="파일 쓰기·명령 실행을 매번 물어볼지" value={options.approve}
            onChange={(e) => setOption('approve', e.target.value)}>
            <option value="none">승인: 매번 묻기</option>
            <option value="writes">승인: 파일 쓰기 자동</option>
            <option value="all">승인: 전부 자동</option>
          </select>
          <select className="ghost-select" title="모델을 몇 번까지 부를지. 무제한이면 시간 제한도 없음" value={options.maxSteps}
            onChange={(e) => setOption('maxSteps', e.target.value)}>
            <option value="20">스텝 20</option>
            <option value="50">스텝 50</option>
            <option value="100">스텝 100</option>
            <option value="0">스텝 무제한</option>
          </select>
          <span className="spacer" />
          <button type="button" className={`send-btn${running ? ' running' : ''}`} disabled={!running && !modelReady}
            title={running ? '중지' : modelReady ? '보내기 (Enter)' : '모델이 준비되면 보낼 수 있습니다'} aria-label={running ? '중지' : '보내기'}
            onClick={running ? onStop : onSend}>
            {running ? <svg viewBox="0 0 20 20" width="14" height="14"><rect x="4" y="4" width="12" height="12" rx="2" fill="currentColor" /></svg>
              : <Icon name="send" size={18} />}
          </button>
        </div>
      </div>
      <div className={`composer-note${allAuto ? ' warn' : ''}`}>
        {allAuto ? '전부 자동: 명령 실행·패키지 설치·API 변경까지 묻지 않고 실행합니다. 믿을 수 있는 작업에만 쓰세요.'
          : '에이전트는 실수할 수 있습니다. 승인하기 전에 내용을 확인하세요.'}
      </div>
    </>
  );
}
