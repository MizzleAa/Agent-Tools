// 관리 › 접속 설정: 로그인 사용 켜기(첫 관리자) · 끄기 · 다른 PC 가 들어올 주소 · 비밀번호 변경 주기 · 로그인 유지 시간 · 폐쇄망 스위치
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { api, type Schemas } from '@/api';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { Field, Section, Toggle } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';

type View = Schemas['AccessView'];

export function AccessTab() {
  const ui = useUi();
  const app = useApp();
  const auth = useAuth();
  const [view, setView] = useState<View | null>(null);
  const [draft, setDraft] = useState<View | null>(null);
  const [host, setHost] = useState('');
  const load = useCallback(async () => {
    try { const v = await api.admin.access(); setView(v); setDraft(v); } catch (e) { ui.error(e); }
  }, [ui]);
  useEffect(() => { load(); }, [load]);
  if (!view || !draft) return <div className="muted">불러오는 중…</div>;

  const save = async (next: View, note = '저장했습니다') => {
    try {
      const v = await api.admin.saveAccess({ app_name: next.app_name, login_enabled: next.login_enabled, public_hosts: next.public_hosts,
        password_max_age_days: next.password_max_age_days, session_hours: next.session_hours, offline: next.offline });
      setView(v); setDraft(v); ui.toast(note, 'ok');
      app.refreshStatus();
      await auth.refresh();
    } catch (e) { ui.error(e); }
  };
  const addHost = (h: string) => {
    const v = h.trim().toLowerCase();
    if (v && !draft.public_hosts.includes(v)) setDraft({ ...draft, public_hosts: [...draft.public_hosts, v] });
    setHost('');
  };
  const turnOff = async () => {
    if (!(await ui.confirm({ title: '로그인 사용을 끌까요?', danger: true, confirmText: '끄기',
      message: '로그인 없이 쓰는 혼자 쓰기로 바꿉니다 (기본은 로그인 먼저). 접속 주소에 남아 있는 다른 PC 에서도 로그인 없이 모든 기능을 쓸 수 있게 되므로, 필요하면 접속 주소도 비우세요. 계정·기록은 그대로 남고, 다시 켜면 돌아옵니다.' }))) return;
    save({ ...draft, login_enabled: false }, '로그인 사용을 껐습니다');
  };
  const dirty = JSON.stringify({ ...draft, login_enabled: 0 }) !== JSON.stringify({ ...view, login_enabled: 0 });
  const urls = draft.public_hosts.map((h) => `http://${h}:${view.port}`);
  return (
    <>
      <Section title="로그인 사용" right={view.login_enabled ? <button type="button" className="btn danger" onClick={turnOff}>로그인 끄기</button> : null}>
        {view.login_enabled ? (
          <div className="muted small" style={{ marginTop: 8 }}>
            켜져 있습니다. 관리자가 발급한 계정으로 로그인해야 쓸 수 있고, 각자 자기 대화·작업 폴더만 봅니다.
            관리자 비밀번호를 잊었을 때는 서버 PC 에서 <code className="inline">.venv\Scripts\python -m server --reset-password 아이디</code>
            (또는 <code className="inline">--login off</code> 로 로그인 끄기).
          </div>
        ) : <SetupLogin hasAdmin={auth.state.has_admin} onDone={async (s) => { auth.setState(s); await load(); app.refreshStatus(); }} />}
      </Section>
      <Section title="서비스 이름" help="로그인 화면 · 사이드바 · 브라우저 탭에 보이는 이름">
        <div className="field-grid">
          <Field label="이름"><input type="text" maxLength={40} value={draft.app_name} onChange={(e) => setDraft({ ...draft, app_name: e.target.value })} /></Field>
        </div>
      </Section>
      <Section title="접속 주소" help="다른 PC 가 이 서버에 들어올 때 쓰는 주소(IP·도메인)입니다. 여기 없는 주소로 들어오면 막습니다. 이 PC 자신(127.0.0.1·localhost)은 늘 됩니다.">
        <div className="chips-row">
          {draft.public_hosts.map((h) => (
            <span key={h} className="chip mono">{h} <button type="button" className="chip-x" aria-label={`${h} 빼기`}
              onClick={() => setDraft({ ...draft, public_hosts: draft.public_hosts.filter((x) => x !== h) })}>×</button></span>
          ))}
          {draft.public_hosts.length === 0 && <span className="muted small">없음 — 이 PC 에서만 들어올 수 있습니다.</span>}
        </div>
        <div className="inline-add">
          <input type="text" value={host} placeholder="예: 192.168.0.10 또는 agent.company.co.kr" onChange={(e) => setHost(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addHost(host); } }} />
          <button type="button" className="btn" onClick={() => addHost(host)}>추가</button>
        </div>
        {view.server_addresses.filter((a) => !draft.public_hosts.includes(a.toLowerCase())).length > 0 && (
          <div className="muted small" style={{ marginTop: 8 }}>이 서버 PC 의 주소: {view.server_addresses.filter((a) => !draft.public_hosts.includes(a.toLowerCase())).map((a) => (
            <button key={a} type="button" className="chip chip-btn" onClick={() => addHost(a)}>＋ {a}</button>))}</div>
        )}
        {urls.length > 0 && <div className="muted small" style={{ marginTop: 8 }}>고객사에 알려 줄 주소: {urls.map((u) => <code key={u} className="inline">{u}</code>)}
          {!view.login_enabled && <b> · 로그인 사용이 꺼져 있어 이 주소로 들어온 사람도 로그인 없이 모든 기능을 씁니다.</b>}</div>}
      </Section>
      <Section title="비밀번호 · 로그인 유지">
        <div className="field-grid">
          <Field label="비밀번호 변경 안내 (일)" help="마지막으로 바꾼 지 이 날수가 지나면 로그인할 때 바꾸라고 안내합니다 ('나중에' 가능). 0 = 안내 안 함">
            <input type="number" min={0} max={3650} value={draft.password_max_age_days}
              onChange={(e) => setDraft({ ...draft, password_max_age_days: Number(e.target.value) })} /></Field>
          <Field label="로그인 유지 (시간)" help="쓰지 않고 이 시간이 지나면 다시 로그인합니다">
            <input type="number" min={1} max={720} value={draft.session_hours}
              onChange={(e) => setDraft({ ...draft, session_hours: Number(e.target.value) })} /></Field>
        </div>
      </Section>
      <Section title="폐쇄망 스위치">
        <Toggle checked={draft.offline} onChange={(v) => setDraft({ ...draft, offline: v })} label="외부 AI 모델 끄기"
          help="켜면 OpenAI·Claude·Gemini 연결을 하나도 시도하지 않고 입력창에서도 사라집니다. 로컬 모델만 씁니다." />
      </Section>
      <div className="save-row">
        <button type="button" className="btn primary" disabled={!dirty || !draft.app_name.trim()} onClick={() => save(draft)}>저장</button>
        {dirty && <button type="button" className="btn" onClick={() => setDraft(view)}>되돌리기</button>}
      </div>
    </>
  );
}

function SetupLogin({ hasAdmin, onDone }: { hasAdmin: boolean; onDone: (s: Schemas['AuthState']) => void }) {
  const ui = useUi();
  const [username, setUsername] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [again, setAgain] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!hasAdmin && password !== again) { ui.toast('비밀번호 두 칸이 서로 다릅니다', 'error'); return; }
    if (!(await ui.confirm({ title: '로그인 사용을 켤까요?', confirmText: '켜기',
      message: hasAdmin ? '기존 관리자 계정으로 로그인하면서 켭니다.'
        : '이 계정이 첫 관리자가 되고, 지금까지의 대화 기록은 이 관리자 것이 됩니다. 켠 뒤에는 발급한 계정으로 로그인해야 쓸 수 있습니다.' }))) return;
    setBusy(true);
    try { onDone(await api.auth.setup({ username, password, display_name: name })); ui.toast('로그인 사용을 켰습니다', 'ok'); }
    catch (err) { ui.error(err); } finally { setBusy(false); }
  };
  return (
    <form onSubmit={submit}>
      <div className="muted small" style={{ marginTop: 8 }}>
        꺼져 있습니다 (혼자 쓰기 — 들어온 사람은 로그인 없이 관리자로 모든 기능을 씁니다). 켜면 들어오는 사람은 모두 로그인 화면부터 보고,
        관리자가 고객사별로 계정을 발급해 각자 자기 대화·작업 폴더만 봅니다.
        {hasAdmin ? ' 예전에 만든 관리자 계정이 있습니다 — 그 아이디·비밀번호를 넣으세요.' : ' 먼저 관리자 계정을 만드세요.'}
      </div>
      <div className="field-grid">
        <Field label="관리자 아이디"><input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></Field>
        {!hasAdmin && <Field label="이름"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>}
        <Field label={hasAdmin ? '비밀번호' : '비밀번호 (8자 이상)'}><input type="password" autoComplete={hasAdmin ? 'current-password' : 'new-password'} value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
        {!hasAdmin && <Field label="비밀번호 확인"><input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></Field>}
      </div>
      <button type="submit" className="btn primary" style={{ marginTop: 12 }} disabled={busy}>{busy ? '켜는 중…' : '로그인 사용 켜기'}</button>
    </form>
  );
}
