// 로그인 화면 (기본 — 들어오면 로그인부터) + 비밀번호 찾기 (관리자에게 재설정 요청 — 메일을 보내지 않는다)
// 관리자 계정이 아직 없으면(처음 설치) 로그인 대신 '첫 관리자 만들기' — 서버 PC 에서만 (다른 PC 에서는 안내만)
import { type FormEvent, useState } from 'react';
import { api, errorText, type Schemas } from '@/api';

export function LoginView({ state, onLoggedIn }: { state: Schemas['AuthState']; onLoggedIn: (s: Schemas['AuthState']) => void }) {
  if (!state.has_admin) return <FirstAdmin state={state} onDone={onLoggedIn} />;
  return <Login state={state} onLoggedIn={onLoggedIn} />;
}

function FirstAdmin({ state, onDone }: { state: Schemas['AuthState']; onDone: (s: Schemas['AuthState']) => void }) {
  const [username, setUsername] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [again, setAgain] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    if (password.length < 8) { setError('비밀번호는 8자 이상이어야 합니다'); return; }
    if (password !== again) { setError('비밀번호 두 칸이 서로 다릅니다'); return; }
    setBusy(true);
    try { onDone(await api.auth.setup({ username: username.trim(), password, display_name: name.trim() })); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };
  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand">{state.app_name}</div>
        <h2 className="auth-title">첫 관리자 만들기</h2>
        {state.setup_allowed ? (
          <form className="auth-form" onSubmit={submit}>
            <div className="muted small">처음 설치한 서버입니다. 관리자 계정을 만들면 그 계정으로 바로 로그인되고, 이후 들어오는 사람은 모두 로그인 화면부터 봅니다.
              고객사 계정은 관리자가 '관리 › 사용자 · 회사'에서 발급합니다.</div>
            <label className="set-field"><span className="set-label">관리자 아이디</span>
              <input type="text" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
            <label className="set-field"><span className="set-label">이름</span>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="화면에 보일 이름" /></label>
            <label className="set-field"><span className="set-label">비밀번호 (8자 이상)</span>
              <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
            <label className="set-field"><span className="set-label">비밀번호 확인</span>
              <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></label>
            {error && <div className="notice error"><span className="msg">{error}</span></div>}
            <button type="submit" className="btn primary" disabled={busy}>{busy ? '만드는 중…' : '관리자 만들고 시작하기'}</button>
          </form>
        ) : (
          <div className="auth-form">
            <div className="muted small">아직 관리자 계정이 없습니다. 관리자가 <b>서버 PC</b>의 브라우저로 <code className="inline">http://127.0.0.1:8765</code> 에
              들어가 첫 관리자 계정을 만들어야 합니다 (다른 PC 에서는 만들 수 없습니다).</div>
          </div>
        )}
      </div>
    </div>
  );
}

function Login({ state, onLoggedIn }: { state: Schemas['AuthState']; onLoggedIn: (s: Schemas['AuthState']) => void }) {
  const [mode, setMode] = useState<'login' | 'forgot' | 'sent'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [company, setCompany] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const login = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try { onLoggedIn(await api.auth.login(username.trim(), password)); } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };
  const forgot = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await api.auth.requestReset({ username: username.trim(), company: company.trim(), note: note.trim() });
      setMode('sent');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand">{state.app_name}</div>
        {mode === 'login' && (
          <form className="auth-form" onSubmit={login}>
            <h2 className="auth-title">로그인</h2>
            <label className="set-field"><span className="set-label">아이디</span>
              <input type="text" autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
            <label className="set-field"><span className="set-label">비밀번호</span>
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
            {error && <div className="notice error"><span className="msg">{error}</span></div>}
            <button type="submit" className="btn primary" disabled={busy}>{busy ? '확인 중…' : '로그인'}</button>
            <button type="button" className="btn link" onClick={() => { setMode('forgot'); setError(''); }}>비밀번호를 잊었어요</button>
            <div className="muted small">계정은 관리자가 발급합니다. 계정이 없으면 관리자에게 요청하세요.</div>
          </form>
        )}
        {mode === 'forgot' && (
          <form className="auth-form" onSubmit={forgot}>
            <h2 className="auth-title">비밀번호 찾기</h2>
            <div className="muted small">관리자에게 재설정을 요청합니다. 관리자가 임시 비밀번호를 만들어 따로 알려 주면, 그것으로 로그인한 뒤 새 비밀번호로 바꾸세요.</div>
            <label className="set-field"><span className="set-label">아이디</span>
              <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
            <label className="set-field"><span className="set-label">소속 회사</span>
              <input type="text" value={company} onChange={(e) => setCompany(e.target.value)} placeholder="관리자가 알아볼 수 있게" /></label>
            <label className="set-field"><span className="set-label">남길 말 (연락처 등)</span>
              <input type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="예: 내선 1234 로 알려 주세요" /></label>
            {error && <div className="notice error"><span className="msg">{error}</span></div>}
            <button type="submit" className="btn primary" disabled={busy}>{busy ? '보내는 중…' : '관리자에게 요청하기'}</button>
            <button type="button" className="btn link" onClick={() => setMode('login')}>로그인으로 돌아가기</button>
          </form>
        )}
        {mode === 'sent' && (
          <div className="auth-form">
            <h2 className="auth-title">요청했습니다</h2>
            <div className="muted small">관리자가 확인하면 임시 비밀번호를 알려 줍니다. 그 비밀번호로 로그인하면 새 비밀번호를 정하는 화면이 나옵니다.</div>
            <button type="button" className="btn" onClick={() => { setMode('login'); setPassword(''); }}>로그인으로 돌아가기</button>
          </div>
        )}
      </div>
    </div>
  );
}
