// 접속 방식과 로그인한 사람. 로그인 사용 모드면 로그인 전에는 로그인 화면만, 임시 비밀번호면 변경 화면만 보여 준다.
// 비밀번호를 바꾼 지 30일(설정값)이 지났으면 들어올 때 한 번 바꾸라고 안내한다 ('나중에' — 이번 로그인 동안은 다시 묻지 않음).
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from 'react';
import { api, AUTH_EVENT, type Schemas } from '@/api';
import { ChangePasswordForm } from '@/features/auth/ChangePasswordForm';
import { LoginView } from '@/features/auth/LoginView';
import { brand, pageTitle } from '@/lib/brand';
import { Modal } from '@/ui/Modal';

export type AuthState = Schemas['AuthState'];
export type UserInfo = Schemas['UserInfo'];

interface AuthCtx {
  state: AuthState;
  user: UserInfo | null;          // null = 혼자 쓰기 (로그인 없음)
  isAdmin: boolean;               // 혼자 쓰기에서는 이 PC 사람 = 관리자
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  setState: (s: AuthState) => void;
}

const Ctx = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error('AuthGate 밖');
  return v;
}

const DISMISS_KEY = 'agent.pw-later';

function dismissed(userId: string): boolean {
  try { return sessionStorage.getItem(DISMISS_KEY) === userId; } catch { return false; }
}

/** 로그인 상태를 읽고, 들어올 수 있을 때만 children(앱 전체)을 그린다 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState | null>(null);
  const [failed, setFailed] = useState(false);
  const [later, setLater] = useState(false);

  const refresh = useCallback(async () => {
    try { setState(await api.auth.me()); setFailed(false); } catch { setFailed(true); }
  }, []);
  useEffect(() => {   // 서비스 이름 (관리 › 접속 설정)
    if (!state) return;
    brand.name = state.app_name;
    if (!state.login_enabled || state.user) return;
    document.title = pageTitle('로그인');
  }, [state]);
  useEffect(() => {
    refresh();
    const onAuth = () => { refresh(); };
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, [refresh]);

  const logout = useCallback(async () => {
    try { await api.auth.logout(); } catch { /* 이미 끝난 세션 */ }
    try { sessionStorage.removeItem(DISMISS_KEY); } catch { /* */ }
    await refresh();
  }, [refresh]);

  if (!state) {
    return (
      <div className="auth-page"><div className="auth-card">
        <div className="brand">{brand.name}</div>
        <div className="muted small">{failed ? '관리 서버에 연결할 수 없습니다. 서버가 켜져 있는지 확인하세요.' : '불러오는 중…'}</div>
        {failed && <button type="button" className="btn" onClick={refresh}>다시 시도</button>}
      </div></div>
    );
  }
  const user = state.user ?? null;
  if (state.login_enabled && !user) return <LoginView state={state} onLoggedIn={setState} />;
  if (user?.must_change) {
    return (
      <div className="auth-page"><div className="auth-card">
        <div className="brand">{state.app_name}</div>
        <h2 className="auth-title">새 비밀번호를 정하세요</h2>
        <div className="muted small">관리자가 발급한 임시 비밀번호로 들어왔습니다. 새 비밀번호로 바꾼 뒤에 쓸 수 있습니다.</div>
        <ChangePasswordForm onDone={setState} />
        <button type="button" className="btn link" onClick={logout}>로그아웃</button>
      </div></div>
    );
  }
  const value: AuthCtx = { state, user, isAdmin: !user || user.role === 'admin', refresh, logout, setState };
  const remind = !!user?.password_expired && !later && !dismissed(user.id);
  return (
    <Ctx.Provider value={value}>
      {children}
      {remind && user && (
        <Modal onClose={() => setLater(true)} labelledBy="pw-expired">
          <h2 className="modal-title" id="pw-expired">비밀번호를 바꿀 때가 됐습니다</h2>
          <div className="muted small">마지막으로 바꾼 지 {state.password_max_age_days}일이 지났습니다. 새 비밀번호로 바꿔 주세요.</div>
          <ChangePasswordForm onDone={(s) => { setState(s); setLater(true); }} />
          <div className="modal-actions">
            <button type="button" className="btn" onClick={() => {
              try { sessionStorage.setItem(DISMISS_KEY, user.id); } catch { /* */ }
              setLater(true);
            }}>나중에</button>
          </div>
        </Modal>
      )}
    </Ctx.Provider>
  );
}
