// 관리 › 외부 AI 모델: 회사(우리 쪽) 공급사 계정(API 키, 또는 Claude 는 회사 계정 로그인)을 연결하면 로그인한 모든 사람이
// 입력창 모델 선택에서 고를 수 있다.
// 키 값은 화면에 다시 보이지 않는다 (끝 4자리만). 폐쇄망 스위치가 켜져 있으면 외부 연결을 하나도 하지 않는다.
// 탭: API 연결 (키·회사 계정) | CLI (이 PC 의 Claude Code — features/claudecode/CcConnection) · 주소 ?view=cli
import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { CcConnection } from '@/features/claudecode/CcConnection';
import { useApp } from '@/state/AppState';
import { Badge, Field, Section } from '@/ui/form';
import { Spinner } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';

type Catalog = Schemas['ProviderCatalog'];
type Provider = Schemas['ProviderInfo'];
type Kind = Provider['kind'];
const EFFORTS: [string, string][] = [['', '공급사 기본'], ['low', '낮음 (빠르고 쌈)'], ['medium', '보통'], ['high', '높음 (꼼꼼, 비쌈)'],
  ['xhigh', '아주 높음 (Claude)'], ['max', '최대 (Claude)']];

type LoginState = Schemas['LoginState'];

/** Claude 회사 계정 로그인 — 버튼을 누르면 서버가 Anthropic 공식 로그인 도구를 띄운다.
 *  서버 PC 에서 누르면 그 PC 에 브라우저가 열리고, 다른 PC 에서 누르면 로그인 주소를 열고 받은 코드를 여기에 붙여 넣는다. */
function LoginPanel({ p, ready, autoStart, onDone }: { p: Provider; ready: boolean; autoStart?: boolean; onDone: () => void }) {
  const ui = useUi();
  const [st, setSt] = useState<LoginState | null>(null);
  const [code, setCode] = useState('');
  const started = useRef(false);
  const start = useCallback(async () => {
    try { setSt(await api.admin.startLogin(p.id)); } catch (e) { ui.error(e); }
  }, [p.id, ui]);
  useEffect(() => {   // 이미 진행 중인 로그인이 있으면 이어서 보여 준다
    api.admin.loginStatus(p.id).then((s) => setSt(s.state === 'idle' ? null : s)).catch(() => {});
  }, [p.id]);
  useEffect(() => {
    if (autoStart && ready && !started.current) { started.current = true; start(); }
  }, [autoStart, ready, start]);
  useEffect(() => {
    if (st?.state !== 'running') return;
    const t = setTimeout(async () => {
      try {
        const next = await api.admin.loginStatus(p.id);
        setSt(next);
        if (next.state === 'done') { ui.toast('Claude 계정으로 로그인했습니다', 'ok'); setTimeout(onDone, 1500); }
      } catch { /* 다음에 다시 */ }
    }, 1500);
    return () => clearTimeout(t);
  }, [st, p.id, ui, onDone]);
  const sendCode = async () => {
    if (!code.trim()) return;
    try { setSt(await api.admin.loginCode(p.id, code)); setCode(''); } catch (e) { ui.error(e); }
  };
  const running = st?.state === 'running';
  return (
    <div className="login-steps">
      {!ready && <div className="notice error"><span className="msg">로그인 도구(runtime\ant\ant.exe — Anthropic 공식 CLI)가 없습니다. github.com/anthropics/anthropic-cli 의 Windows 배포본을 그 자리에 풀어 두세요.</span></div>}
      <div className="cmd-row">
        {!running && <button type="button" className="btn primary" disabled={!ready} onClick={start}>
          {p.check_error || !p.models.length ? 'Claude 계정으로 로그인' : '다시 로그인 (계정 바꾸기)'}</button>}
        {running && <><Spinner /><span className="small">{st?.message}</span>
          <button type="button" className="btn" onClick={async () => { await api.admin.cancelLogin(p.id).catch(() => {}); setSt(null); }}>그만두기</button></>}
        {st && !running && <span className={`small ${st.state === 'done' ? '' : 'err-text'}`}>{st.message}</span>}
      </div>
      {running && st?.mode === 'manual' && (
        <div className="login-manual">
          {st.urls.length ? st.urls.map((u) => <a key={u} className="btn" href={u} target="_blank" rel="noreferrer">로그인 창 열기 ↗</a>)
            : <span className="muted small">로그인 주소를 받는 중…</span>}
          <div className="inline-add">
            <input type="text" value={code} placeholder="로그인을 마치면 나오는 코드를 붙여 넣으세요" onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); sendCode(); } }} />
            <button type="button" className="btn primary" onClick={sendCode}>보내기</button>
          </div>
        </div>
      )}
      {st && st.output.length > 0 && (
        <details className="small"><summary className="muted">로그인 도구 출력</summary><pre className="login-output">{st.output.join('\n')}</pre></details>
      )}
    </div>
  );
}

export function ProvidersTab() {
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'cli' ? 'cli' : 'api';
  return (
    <>
      <div className="stab-group cc-subtabs">
        {([['api', 'API'], ['cli', 'CLI']] as const).map(([id, label]) => (
          <button key={id} type="button" className={`stab${view === id ? ' active' : ''}`} onClick={() => setParams(id === 'cli' ? { view: 'cli' } : {})}>{label}</button>
        ))}
      </div>
      {view === 'cli' ? <CcConnection /> : <ApiProviders />}
    </>
  );
}

function ApiProviders() {
  const ui = useUi();
  const app = useApp();
  const [cat, setCat] = useState<Catalog | null>(null);
  const load = useCallback(async () => { try { setCat(await api.admin.providers()); } catch (e) { ui.error(e); } }, [ui]);
  useEffect(() => { load(); }, [load]);
  const changed = async () => { await load(); app.refreshStatus(); };   // 입력창 모델 목록도 새로
  const [loginFor, setLoginFor] = useState('');   // 방금 '회사 계정 로그인'으로 추가한 연결 — 바로 로그인을 시작한다
  if (!cat) return <div className="muted">불러오는 중…</div>;
  const offline = app.status?.access.offline;
  return (
    <>
      {offline && <div className="notice error"><span className="msg">폐쇄망 스위치가 켜져 있어 외부 모델을 쓰지 않습니다 (접속 설정에서 끌 수 있음).</span></div>}
      <AddProvider cat={cat} onAdded={(id) => { if (id) setLoginFor(id); changed(); }} />
      {cat.items.map((p) => <ProviderCard key={p.id} p={p} onChange={changed} loginReady={cat.login_tool_ready} autoLogin={p.id === loginFor} />)}
      {cat.items.length === 0 && <div className="muted small" style={{ margin: '8px 2px' }}>아직 연결한 외부 모델이 없습니다.</div>}
    </>
  );
}

function AddProvider({ cat, onAdded }: { cat: Catalog; onAdded: (loginId?: string) => void }) {
  const ui = useUi();
  const [kind, setKind] = useState<Kind>('anthropic');
  const [auth, setAuth] = useState<'api_key' | 'account_login'>('api_key');
  const [title, setTitle] = useState('');
  const [key, setKey] = useState('');   // API 키 (계정 로그인이면 쓰지 않는다)
  const [baseUrl, setBaseUrl] = useState('');
  const [effort, setEffort] = useState('');
  const [profile, setProfile] = useState('agent');
  const [busy, setBusy] = useState(false);
  const info = cat.kinds.find((k) => k.kind === kind)!;
  const login = auth === 'account_login';
  const canSubmit = login ? info.account_login && !!profile.trim() : !!key;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const p = await api.admin.addProvider({ kind, title, auth, api_key: login ? '' : key, base_url: baseUrl, effort, profile: login ? profile.trim() : '' });
      if (login) ui.toast('연결을 만들었습니다 — 이어서 Claude 계정으로 로그인합니다', 'ok', 5000);
      else ui.toast(p.check_error ? `연결은 저장했지만 확인에 실패했습니다: ${p.check_error}` : `연결했습니다 — 모델 ${p.models.length}개`, p.check_error ? 'error' : 'ok', 7000);
      setKey(''); setTitle(''); setBaseUrl('');
      onAdded(login ? p.id : undefined);
    } catch (err) { ui.error(err); } finally { setBusy(false); }
  };
  return (
    <Section title="외부 모델 연결 추가" help="회사용 API 키를 넣거나 (Claude 는) 회사 계정으로 로그인하면, 실제로 연결해 보고 쓸 수 있는 모델 목록을 받아 옵니다. 처음에는 최신 모델 3개만 켜 둡니다.">
      <form onSubmit={submit}>
        <div className="field-grid">
          <Field label="공급처">
            <select className="select" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              {cat.kinds.map((k) => <option key={k.kind} value={k.kind}>{k.title}</option>)}
            </select>
          </Field>
          <Field label="연결 방식">
            <select className="select" value={auth} onChange={(e) => setAuth(e.target.value as 'api_key' | 'account_login')}>
              <option value="api_key">API 키</option>
              <option value="account_login">회사 계정 로그인{info.account_login ? '' : ' (이 공급처는 불가)'}</option>
            </select>
          </Field>
          <Field label="이름" help="입력창에 보일 이름 (비우면 공급처 이름)"><input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={info.title} /></Field>
          <Field label="추론 강도"><select className="select" value={effort} onChange={(e) => setEffort(e.target.value)}>{EFFORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
        </div>
        {login ? (
          <>
            <div className={`notice${info.account_login ? '' : ' error'}`} style={{ marginTop: 12 }}><span className="msg">{info.account_login_note}</span></div>
            {info.account_login && (
              <>
                <div className="field-grid">
                  <Field label="로그인 프로필 이름" help="연결마다 다른 이름이면 여러 회사 계정을 따로 붙일 수 있습니다">
                    <input type="text" value={profile} onChange={(e) => setProfile(e.target.value.replace(/[^A-Za-z0-9_-]/g, ''))} /></Field>
                </div>
                <div className="muted small" style={{ marginTop: 8 }}>'추가하고 로그인'을 누르면 바로 로그인 창이 열립니다.</div>
              </>
            )}
          </>
        ) : (
          <div className="field-grid">
            <Field label="API 키" help={<>발급: <a href={info.key_page} target="_blank" rel="noreferrer">{info.key_page}</a> · 저장 뒤에는 끝 4자리만 보입니다</>}>
              <input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} required /></Field>
            <Field label="주소 (고급)" help={`비우면 ${info.default_url} · 회사 게이트웨이·프록시를 쓸 때만`}>
              <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={info.default_url} /></Field>
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <button type="submit" className="btn primary" disabled={busy || !canSubmit}>{busy ? '연결하는 중…' : login ? '추가하고 로그인' : '연결하고 확인'}</button>
        </div>
      </form>
    </Section>
  );
}

function ProviderCard({ p, onChange, loginReady, autoLogin }: { p: Provider; onChange: () => void; loginReady: boolean; autoLogin?: boolean }) {
  const ui = useUi();
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await fn(); if (ok) ui.toast(ok, 'ok'); } catch (e) { ui.error(e); } finally { setBusy(false); onChange(); }
  };
  const check = () => run(async () => {
    const r = await api.admin.checkProvider(p.id);
    ui.toast(r.check_error ? `연결 실패: ${r.check_error}` : `연결됨 — 모델 ${r.models.length}개`, r.check_error ? 'error' : 'ok', 7000);
  });
  const newKey = async () => {
    const v = await ui.form({ title: `'${p.title}' 키 바꾸기`, confirmText: '저장하고 확인', fields: [{ name: 'key', label: '새 API 키', type: 'password' }] });
    if (v?.key) run(() => api.admin.updateProvider(p.id, { api_key: v.key }), '키를 바꿨습니다');
  };
  const edit = async () => {
    const v = await ui.form({
      title: `'${p.title}' 고치기`, confirmText: '저장',
      fields: [{ name: 'title', label: '이름', value: p.title }, { name: 'effort', label: '추론 강도', value: p.effort, options: EFFORTS },
        { name: 'base_url', label: '주소 (고급)', value: p.base_url, placeholder: p.default_url, help: '비우면 공급사 기본 주소' }],
    });
    if (v) run(() => api.admin.updateProvider(p.id, { title: v.title, effort: v.effort, base_url: v.base_url }), '저장했습니다');
  };
  const remove = async () => {
    if (await ui.confirm({ title: `'${p.title}' 연결을 지울까요?`, message: '저장한 키도 지워집니다. 지난 기록은 남습니다.', danger: true, confirmText: '지우기' })) {
      run(() => api.admin.deleteProvider(p.id), '지웠습니다');
    }
  };
  const setModel = (id: string, change: { enabled?: boolean; context_length?: number }) =>
    run(() => api.admin.updateProvider(p.id, { models: [{ id, ...change }] }));
  const on = p.models.filter((m) => m.enabled).length;
  return (
    <Section title={<span className="title">{p.title} <span className="muted small">· {p.kind} · {p.auth === 'account_login' ? p.key_hint : `키 ${p.key_hint || '없음'}`}</span></span>}
      right={<>
        {!p.enabled ? <Badge>꺼짐</Badge> : p.check_error ? <Badge kind="fail">연결 실패</Badge> : <Badge kind="ok">연결됨</Badge>}
        <button type="button" className="btn" disabled={busy} onClick={check}>연결 확인</button>
        {p.auth === 'api_key' && <button type="button" className="btn" disabled={busy} onClick={newKey}>키 바꾸기</button>}
        <button type="button" className="btn" disabled={busy} onClick={edit}>고치기</button>
        <button type="button" className="btn" disabled={busy} onClick={() => run(() => api.admin.updateProvider(p.id, { enabled: !p.enabled }))}>{p.enabled ? '끄기' : '켜기'}</button>
        <button type="button" className="btn danger" disabled={busy} onClick={remove}>지우기</button></>}>
      {p.check_error && <div className="notice error" style={{ marginTop: 10 }}><span className="msg">{p.check_error}</span></div>}
      {p.auth === 'account_login' && <LoginPanel p={p} ready={loginReady} autoStart={autoLogin} onDone={onChange} />}
      <div className="muted small" style={{ marginTop: 6 }}>마지막 확인 {p.checked_at || '—'} · 추론 강도 {EFFORTS.find(([v]) => v === p.effort)?.[1] ?? p.effort} · 켠 모델 {on}/{p.models.length}
        {p.base_url && <> · 주소 {p.base_url}</>}</div>
      {p.models.length > 0 && (
        <table className="set-table">
          <thead><tr><th style={{ width: 44 }}>쓰기</th><th>모델</th><th style={{ width: 170 }}>컨텍스트 (토큰)</th></tr></thead>
          <tbody>{p.models.map((m) => (
            <tr key={m.id}>
              <td><input type="checkbox" checked={m.enabled} disabled={busy} onChange={(e) => setModel(m.id, { enabled: e.target.checked })} /></td>
              <td><code className="inline">{m.id}</code>{m.title && m.title !== m.id && <span className="muted small"> {m.title}</span>}</td>
              <td><input type="number" min={2048} step={1024} defaultValue={m.context_length} disabled={busy}
                title="에이전트가 한 번에 보낼 대화 길이 한도. 크게 잡을수록 긴 작업을 덜 잘라내지만 요금이 늘어납니다"
                onBlur={(e) => { const n = Number(e.target.value); if (n && n !== m.context_length) setModel(m.id, { context_length: n }); }} /></td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </Section>
  );
}
