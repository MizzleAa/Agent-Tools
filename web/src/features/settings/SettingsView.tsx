// 설정 · 프롬프트. 주소로 탭을 고른다:
//   /settings/p/<용도>  /settings/w/<워크플로>  /settings/models  /settings/blocks[/<id>]  /settings/messages  /settings/tools
//   /settings/variables  /settings/params  /settings/standards[/<파일>]  /settings/history  /settings/guide
//   /settings/harness[/<하네스>][?open=<파일>]  하네스 (마크다운 지침 · 용도 · 자료 — features/settings/HarnessTab.tsx)
//                                     이름 없음 = 공통, 있으면 회사·프로젝트 하네스 (harnesses/<이름>)
import { useCallback, useEffect, useState } from 'react';
import { Navigate, useBlocker, useNavigate, useParams } from 'react-router-dom';
import { api } from '@/api';
import { localGet, localSet } from '@/lib/storage';
import { useApp } from '@/state/AppState';
import { useTitle } from '@/state/title';
import { Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { FileTabsGuide } from './FileTabsGuide';
import { HarnessTab } from './HarnessTab';
import { ModelsTab } from './ModelsTab';
import { ParamsTab } from './ParamsTab';
import { PendingProvider, SaveBar, usePending } from './pending';
import { type Bundle, SettingsCtx, useSettings } from './parts';
import { newProfile, ProfileTab } from './ProfileTab';
import { BlocksTab, MessagesTab, ToolsTab, VariablesTab } from './PromptTabs';
import { newWorkflow, WorkflowTab } from './WorkflowTab';

const COMMON: [string, string][] = [
  ['models', '모델'], ['blocks', '공통 블록'], ['messages', '루프 메시지'], ['tools', '도구 설명'], ['variables', '변수'],
  ['params', '실행 파라미터'], ['standards', '개발 규격'], ['history', '변경 기록'], ['guide', '안내'],
];

export function SettingsView() {
  useTitle('설정 · 프롬프트');
  const ui = useUi();
  const app = useApp();
  const [data, setData] = useState<Bundle | null>(null);
  const [version, setVersion] = useState(0);   // 다시 읽거나 되돌리면 올려서 탭의 초안을 새로 만든다
  const reload = useCallback(async () => {
    try { setData(await api.settings.get()); setVersion((v) => v + 1); } catch (e) { ui.error(e); }
  }, [ui]);
  const refresh = useCallback(async () => {   // 하네스 파일을 고친 뒤: 목록(워크플로 노드의 용도 등)만 새로 — 열린 탭은 그대로
    try { setData(await api.settings.get()); } catch { /* 다음에 다시 읽을 때 */ }
  }, []);
  useEffect(() => { reload(); }, [reload]);
  const onSaved = useCallback(async () => {
    await reload();
    await app.reloadCatalog();   // 입력창의 용도·워크플로 목록도 새로
    app.refreshStatus();         // 입력창의 모델 목록도 새로
  }, [reload, app]);
  if (!data) return <section className="view active"><div className="scroller"><div className="page settings-page"><div className="muted">불러오는 중…</div></div></div></section>;
  return (
    <SettingsCtx.Provider value={{ data, reload }}>
      <PendingProvider onSaved={onSaved}>
        <Inner version={version} bump={() => setVersion((v) => v + 1)} reload={reload} refresh={refresh} />
      </PendingProvider>
    </SettingsCtx.Provider>
  );
}

function Inner({ version, bump, reload, refresh }: {
  version: number; bump: () => void; reload: () => Promise<void>; refresh: () => Promise<void>;
}) {
  const { tab, name } = useParams();
  const { data } = useSettings();
  const { items, clear } = usePending();
  const ui = useUi();
  const navigate = useNavigate();
  const app = useApp();

  // 저장 안 한 게 있으면 다른 탭·화면으로 가기 전에 묻는다
  const blocker = useBlocker(({ currentLocation, nextLocation }) => items.size > 0 && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    ui.confirm({ title: '저장하지 않은 변경이 있습니다', message: '저장하지 않고 이동하면 바꾼 내용이 사라집니다.', confirmText: '버리고 이동', danger: true })
      .then((ok) => { if (ok) { clear(); blocker.proceed(); } else blocker.reset(); });
  }, [blocker.state]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (tab) localSet('settings.tab', name ? `${tab}/${name}` : tab); }, [tab, name]);

  if (!tab) {
    const saved = localGet('settings.tab')?.replace(/^([pw]):/, '$1/');   // 예전 화면은 'p:coder' 로 기억했다
    const ok = saved && (/^harness(\/[a-z0-9_-]+)?$/.test(saved) || /^[pw]\/[a-z0-9_-]+$/.test(saved) || COMMON.some(([id]) => saved === id || saved.startsWith(`${id}/`)));
    return <Navigate to={`/settings/${ok ? saved : `p/${data.profiles[0]?.name ?? 'general'}`}`} replace />;
  }
  if (!['p', 'w', 'harness', ...COMMON.map(([id]) => id)].includes(tab)) return <Navigate to="/settings/guide" replace />;
  const after = async () => { clear(); await reload(); await app.reloadCatalog(); };
  const profile = tab === 'p' ? data.profiles.find((p) => p.name === name) : undefined;
  const workflow = tab === 'w' ? data.workflows.find((w) => w.name === name) : undefined;
  const scopeInfo = tab === 'harness' && name ? data.harnesses.find((h) => h.id === name) : undefined;
  const harnessChanged = async () => { await refresh(); await app.reloadCatalog(); };
  let body;
  if (tab === 'harness' && name && !scopeInfo) body = <Missing />;
  else if (tab === 'harness') body = <HarnessTab scope={name ?? ''} info={scopeInfo} onChanged={harnessChanged}
    onEdit={() => scopeInfo && editHarness(ui, scopeInfo, after)}
    onDelete={() => scopeInfo && deleteHarness(ui, scopeInfo, after, navigate)} />;
  else if (tab === 'p' && profile?.harness) body = <FromHarness name={profile.name} file={profile.file} error={profile.error} />;
  else if (tab === 'p') body = profile ? (profile.profile ? <ProfileTab entry={profile} /> : <Broken file={profile.file} error={profile.error} />) : <Missing />;
  else if (tab === 'w') body = workflow ? (workflow.workflow ? <WorkflowTab entry={workflow} /> : <Broken file={workflow.file} error={workflow.error} />) : <Missing />;
  else if (tab === 'models') body = <ModelsTab />;
  else if (tab === 'blocks') body = <BlocksTab selected={name} />;
  else if (tab === 'messages') body = <MessagesTab />;
  else if (tab === 'tools') body = <ToolsTab />;
  else if (tab === 'variables') body = <VariablesTab />;
  else if (tab === 'params') body = <ParamsTab />;
  else body = <FileTabsGuide tab={tab} name={name} />;

  const tabBtn = (to: string, label: string, dot?: 'bad' | 'mod' | null) => {
    const active = `/settings/${to}` === `/settings/${tab}${name && (tab === 'p' || tab === 'w' || tab === 'harness') ? `/${name}` : ''}`;
    return (
      <button key={to} type="button" className={`stab${active ? ' active' : ''}`} onClick={() => navigate(`/settings/${to}`)}>
        {label}{dot && <span className={`stab-dot${dot === 'bad' ? ' bad' : ''}`} title={dot === 'bad' ? '파일 오류' : '기본값에서 바뀜'} />}
      </button>
    );
  };
  return (
    <section className="view active" id="view-settings">
      <div className="scroller">
        <div className="page settings-page">
          <div className="page-head">
            <div>
              <h1>설정 · 프롬프트</h1>
              <div className="muted small">용도(프로필)마다 프롬프트·도구·파라미터와 공통 프롬프트를 관리합니다. 저장하면 다음 실행부터 적용됩니다.</div>
            </div>
            <button type="button" className="btn" onClick={async () => { if (!items.size || (await ui.confirm({ title: '저장하지 않은 변경을 버리고 다시 읽을까요?', danger: true, confirmText: '다시 읽기' }))) { clear(); reload(); } }}>다시 읽기</button>
          </div>
          <div className="stabs">
            <div className="stab-group"><span className="stab-label">하네스</span>
              {tabBtn('harness', '📁 공통')}
              {data.harnesses.map((h) => tabBtn(`harness/${h.id}`, `${h.kind === 'company' ? '🏢' : '📂'} ${h.title}`))}
              <button type="button" className="stab add" title="회사·프로젝트 단위 하네스 (지침 · 용도 · 자료) 만들기"
                onClick={() => newHarness(ui, after, navigate)}>＋ 새 하네스</button>
            </div>
            <div className="stab-group"><span className="stab-label">용도</span>
              {data.profiles.map((p) => tabBtn(`p/${p.name}`, p.harness ? `📄 ${p.name}` : p.name, p.error ? 'bad' : p.modified ? 'mod' : null))}
              {data.harnesses.flatMap((h) => h.roles.map((r) => (
                <button key={`${h.id}/${r}`} type="button" className="stab" title={`'${h.title}' 하네스의 용도 — 하네스에서 고칩니다`}
                  onClick={() => navigate(`/settings/harness/${h.id}?open=${encodeURIComponent(`agents/${r}.md`)}`)}>
                  {h.kind === 'company' ? '🏢' : '📂'} {h.title} › {r}
                </button>
              )))}
              <button type="button" className="stab add" title="새 용도(프로필) 만들기" onClick={() => newProfile(ui, data, after, navigate)}>＋ 새 용도</button>
            </div>
            <div className="stab-group"><span className="stab-label">워크플로</span>
              {data.workflows.map((w) => tabBtn(`w/${w.name}`, `⛓ ${w.name}`, w.error ? 'bad' : w.modified ? 'mod' : null))}
              <button type="button" className="stab add" title="노드를 이어 붙인 워크플로 만들기" onClick={() => newWorkflow(ui, data, after, navigate)}>＋ 새 워크플로</button>
            </div>
            <div className="stab-group"><span className="stab-label">공통</span>{COMMON.map(([id, label]) => tabBtn(id, label))}</div>
          </div>
          <div key={`${version}-${tab}-${name ?? ''}`}>{body}</div>
        </div>
      </div>
      <SaveBar onDiscard={bump} />
    </section>
  );
}

type HarnessInfo = ReturnType<typeof useSettings>['data']['harnesses'][number];
const KIND_OPTIONS: [string, string][] = [['project', '프로젝트 — 입력창·워크플로 노드에서 고르면 붙는다'], ['company', '회사 — 대상 회사 사람에게는 늘 붙는다']];
const splitNames = (v: string) => v.split(',').map((x) => x.trim()).filter(Boolean);

async function newHarness(ui: ReturnType<typeof useUi>, after: () => Promise<void>, navigate: (to: string) => void) {
  const v = await ui.form({
    title: '새 하네스', confirmText: '만들기',
    message: '회사·프로젝트 단위로 지침 · 용도 · 자료를 따로 둡니다. 실행에는 공통 하네스 + 이 하네스가 합쳐져 붙습니다.',
    fields: [
      { name: 'id', label: '이름 (영어 소문자·숫자·-·_ — 폴더 이름)', placeholder: '예: acme, project-x' },
      { name: 'title', label: '보이는 이름', placeholder: '예: ACME 회사, 프로젝트 X' },
      { name: 'kind', label: '종류', options: KIND_OPTIONS, value: 'project' },
      { name: 'companies', label: '대상 회사 (쉼표로, 비우면 모두)', placeholder: '예: A사' },
    ],
  });
  const id = v?.id.trim().toLowerCase();
  if (!v || !id) return;
  try {
    await api.harness.createScope({ id, title: v.title.trim(), kind: v.kind as 'company' | 'project', companies: splitNames(v.companies), description: '' });
  } catch (e) { ui.error(e); return; }
  ui.toast(`'${v.title.trim() || id}' 하네스를 만들었습니다 — '＋ 새 용도'로 용도를 더하세요.`, 'ok');
  await after();
  navigate(`/settings/harness/${id}`);
}

async function editHarness(ui: ReturnType<typeof useUi>, h: HarnessInfo, after: () => Promise<void>) {
  const v = await ui.form({
    title: `'${h.title}' 하네스 고치기`, confirmText: '저장',
    fields: [
      { name: 'title', label: '보이는 이름', value: h.title },
      { name: 'kind', label: '종류', options: KIND_OPTIONS, value: h.kind },
      { name: 'companies', label: '대상 회사 (쉼표로, 비우면 모두)', value: h.companies.join(', ') },
    ],
  });
  if (!v) return;
  try {
    await api.harness.updateScope(h.id, { title: v.title, kind: v.kind as 'company' | 'project', companies: splitNames(v.companies) });
    await after();
  } catch (e) { ui.error(e); }
}

async function deleteHarness(ui: ReturnType<typeof useUi>, h: HarnessInfo, after: () => Promise<void>, navigate: (to: string) => void) {
  if (!(await ui.confirm({ title: `'${h.title}' 하네스를 지울까요?`, danger: true, confirmText: '지우기',
    message: `harnesses/${h.id} 안의 지침 · 용도(${h.roles.length}개) · 자료가 모두 지워집니다. 이 하네스를 고른 워크플로 노드는 실행 때 멈춥니다. 되돌릴 수 없습니다.` }))) return;
  try { await api.harness.removeScope(h.id); await after(); navigate('/settings/harness'); } catch (e) { ui.error(e); }
}

/** 하네스 역할 파일(harness/agents/<이름>.md)에서 오는 용도 — 여기(yaml)가 아니라 하네스에서 고친다 */
function FromHarness({ name, file, error }: { name: string; file: string; error?: string | null }) {
  const navigate = useNavigate();
  return (
    <Section title={`'${name}' 은 하네스 역할 파일에서 옵니다`}>
      <div className="muted small" style={{ marginTop: 8 }}>
        {file} (마크다운)이 이 용도를 정합니다 — 같은 이름의 예전 설정(yaml)보다 우선합니다. 역할 지침 · 도구는 하네스에서 고치세요.
      </div>
      {error && <div className="notice error" style={{ marginTop: 10 }}><span className="msg">{error}</span></div>}
      <button type="button" className="btn primary" style={{ marginTop: 12 }}
        onClick={() => navigate(`/settings/harness?open=${encodeURIComponent(file.replace(/^harness\//, ''))}`)}>하네스에서 고치기</button>
    </Section>
  );
}

function Missing() {
  return <Section title="없는 항목입니다"><div className="muted small" style={{ marginTop: 8 }}>지워졌거나 이름이 바뀌었습니다. 위 탭에서 고르세요.</div></Section>;
}

function Broken({ file, error }: { file: string; error?: string | null }) {
  return (
    <Section title="파일을 읽을 수 없습니다">
      <div className="notice error" style={{ marginTop: 10 }}><span className="msg">{error}</span></div>
      <div className="muted small" style={{ marginTop: 8 }}>{file} 를 직접 고치거나, '변경 기록'에서 이전 내용으로 되돌리세요.</div>
    </Section>
  );
}
