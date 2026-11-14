// 비밀번호 바꾸기 칸 (임시 비밀번호 변경 · 30일 안내 · 내 계정 화면에서 같이 쓴다)
import { type FormEvent, useState } from 'react';
import { api, errorText, type Schemas } from '@/api';

export function ChangePasswordForm({ onDone }: { onDone: (s: Schemas['AuthState']) => void }) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    if (next.length < 8) { setError('새 비밀번호는 8자 이상이어야 합니다'); return; }
    if (next !== again) { setError('새 비밀번호 두 칸이 서로 다릅니다'); return; }
    setBusy(true);
    try {
      onDone(await api.auth.changePassword(current, next));
      setCurrent(''); setNext(''); setAgain('');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="auth-form" onSubmit={submit}>
      <label className="set-field"><span className="set-label">지금 비밀번호</span>
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></label>
      <label className="set-field"><span className="set-label">새 비밀번호 (8자 이상)</span>
        <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required /></label>
      <label className="set-field"><span className="set-label">새 비밀번호 확인</span>
        <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required /></label>
      {error && <div className="notice error"><span className="msg">{error}</span></div>}
      <button type="submit" className="btn primary" disabled={busy}>{busy ? '바꾸는 중…' : '비밀번호 바꾸기'}</button>
    </form>
  );
}
