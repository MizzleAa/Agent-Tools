// 관리 › 사용자 · 회사: 비밀번호 재설정 요청 · 회사(허용 계정 수 · 쓸 수 있는 용도) · 계정 발급 · 임시 비밀번호 · 사용 중지
import { useCallback, useEffect, useState } from 'react';
import { api, type Schemas } from '@/api';
import { fmtTime } from '@/lib/format';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { Badge, Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';

type User = Schemas['UserInfo'];
type Company = Schemas['CompanyInfo'];

export function UsersTab({ onRequests }: { onRequests: (n: number) => void }) {
  const ui = useUi();
  const app = useApp();
  const { state, user: me } = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [requests, setRequests] = useState<Schemas['ResetRequestInfo'][]>([]);
  const [filter, setFilter] = useState('');
  const load = useCallback(async () => {
    try {
      const [u, c, r] = await Promise.all([api.admin.users(), api.admin.companies(), api.admin.resetRequests(true)]);
      setUsers(u); setCompanies(c); setRequests(r); onRequests(r.length);
    } catch (e) { ui.error(e); }
  }, [ui, onRequests]);
  useEffect(() => { load(); }, [load]);

  const showTemp = (title: string, issued: Schemas['IssuedAccount']) => {
    ui.sheet(title, (
      <div className="temp-pw">
        <div className="muted small">아래 임시 비밀번호를 <b>{issued.user.username}</b> 님에게 직접 전하세요. 이 창을 닫으면 다시 볼 수 없습니다.
          처음 로그인하면 새 비밀번호를 정하는 화면이 나옵니다.</div>
        <div className="temp-pw-value">{issued.temp_password}</div>
        <button type="button" className="btn" onClick={() => navigator.clipboard?.writeText(issued.temp_password)
          .then(() => ui.toast('복사했습니다', 'ok')).catch(() => ui.toast('복사하지 못했습니다 — 직접 옮겨 적으세요', 'error'))}>복사</button>
      </div>
    ));
  };

  const profileNames = app.profiles.map((p) => p.name);
  const editCompany = async (c?: Company) => {
    const v = await ui.form({
      title: c ? `'${c.name}' 고치기` : '회사 만들기', confirmText: '저장',
      fields: [
        { name: 'name', label: '회사 이름', value: c?.name ?? '' },
        { name: 'seats', label: '허용 계정 수', value: c?.seats == null ? '' : String(c.seats), type: 'number',
          help: '그 회사가 요청한 계정 수. 비우면 제한 없음 (넘게 발급하려 하면 막는다)' },
        { name: 'profiles', label: '쓸 수 있는 용도', value: (c?.profiles ?? []).join(', '),
          help: `쉼표로 구분. 비우면 전부. 있는 용도: ${profileNames.join(', ')}` },
        { name: 'note', label: '메모', value: c?.note ?? '' },
      ],
    });
    if (!v) return;
    const profiles = v.profiles.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
    const unknown = profiles.filter((x) => !profileNames.includes(x));
    if (unknown.length) { ui.toast(`없는 용도: ${unknown.join(', ')}`, 'error'); return; }
    const body = { name: v.name, note: v.note, seats: v.seats.trim() === '' ? null : Number(v.seats), profiles };
    try { await (c ? api.admin.updateCompany(c.id, body) : api.admin.createCompany(body)); ui.toast('저장했습니다', 'ok'); } catch (e) { ui.error(e); }
    load();
  };
  const removeCompany = async (c: Company) => {
    if (!(await ui.confirm({ title: `'${c.name}' 회사를 지울까요?`, message: '계정이 남아 있으면 지울 수 없습니다.', danger: true, confirmText: '지우기' }))) return;
    try { await api.admin.deleteCompany(c.id); } catch (e) { ui.error(e); }
    load();
  };

  const issue = async () => {
    const v = await ui.form({
      title: '계정 발급', confirmText: '발급',
      message: '임시 비밀번호를 만들어 보여 줍니다. 받은 사람은 처음 로그인할 때 새 비밀번호를 정합니다.',
      fields: [
        { name: 'username', label: '아이디', placeholder: '영문·숫자 (예: kim.a 또는 이메일)' },
        { name: 'display_name', label: '이름', placeholder: '화면에 보일 이름' },
        { name: 'company_id', label: '회사', options: [['', '(회사 없음)'], ...companies.map((c) => [c.id, `${c.name}${c.seats != null ? ` (${c.issued}/${c.seats})` : ''}`] as [string, string])] },
        { name: 'role', label: '역할', options: [['user', '사용자'], ['admin', '관리자']] },
      ],
    });
    if (!v) return;
    try {
      const issued = await api.admin.createUser({ username: v.username, display_name: v.display_name, company_id: v.company_id, role: v.role as 'user' | 'admin' });
      showTemp('계정을 발급했습니다', issued);
    } catch (e) { ui.error(e); }
    load();
  };
  const editUser = async (u: User) => {
    const v = await ui.form({
      title: `'${u.username}' 고치기`, confirmText: '저장',
      fields: [
        { name: 'display_name', label: '이름', value: u.display_name },
        { name: 'company_id', label: '회사', value: u.company_id, options: [['', '(회사 없음)'], ...companies.map((c) => [c.id, c.name] as [string, string])] },
        { name: 'role', label: '역할', value: u.role, options: [['user', '사용자'], ['admin', '관리자']] },
      ],
    });
    if (!v) return;
    try { await api.admin.updateUser(u.id, { display_name: v.display_name, company_id: v.company_id, role: v.role as 'user' | 'admin' }); } catch (e) { ui.error(e); }
    load();
  };
  const resetPw = async (userId: string, username: string) => {
    if (!(await ui.confirm({ title: `'${username}' 의 비밀번호를 재설정할까요?`, message: '임시 비밀번호를 새로 만들고, 그 사람이 열어 둔 로그인은 모두 끝납니다.', confirmText: '재설정' }))) return;
    try { showTemp('임시 비밀번호를 만들었습니다', await api.admin.resetPassword(userId)); } catch (e) { ui.error(e); }
    load();
  };
  const toggle = async (u: User) => {
    try { await api.admin.updateUser(u.id, { active: !u.active }); } catch (e) { ui.error(e); }
    load();
  };
  const remove = async (u: User) => {
    if (!(await ui.confirm({ title: `'${u.username}' 계정을 지울까요?`, message: '그 사람의 실행 기록과 작업 폴더는 남습니다 (관리 › 기록에서 볼 수 있음). 저장한 사내 API 계정은 지워집니다.', danger: true, confirmText: '지우기' }))) return;
    try { await api.admin.deleteUser(u.id); } catch (e) { ui.error(e); }
    load();
  };
  const dismiss = async (id: string) => {
    try { await api.admin.dismissRequest(id); } catch (e) { ui.error(e); }
    load();
  };

  const company = (id: string) => companies.find((c) => c.id === id)?.name ?? '';
  const shown = users.filter((u) => !filter || u.company_id === filter);
  return (
    <>
      {!state.login_enabled && (
        <div className="notice"><span className="msg">지금은 혼자 쓰기라 계정 없이 쓰고 있습니다. 미리 회사·계정을 만들어 둘 수 있고, '접속 설정'에서 로그인 사용을 켜면 적용됩니다.</span></div>
      )}
      {requests.length > 0 && (
        <Section title={`비밀번호 재설정 요청 ${requests.length}건`} help="로그인 화면의 '비밀번호를 잊었어요'로 들어온 요청입니다. 임시 비밀번호를 만들어 그 사람에게 직접 전하세요 (메일은 보내지 않습니다).">
          <table className="set-table">
            <thead><tr><th>아이디</th><th>회사(적은 것)</th><th>남긴 말</th><th>요청 시각</th><th /></tr></thead>
            <tbody>{requests.map((r) => (
              <tr key={r.id}>
                <td>{r.username}{!r.user_id && <> <Badge kind="warn">없는 아이디</Badge></>}</td>
                <td>{r.company || '—'}</td><td className="small">{r.note || '—'}</td><td className="small">{fmtTime(r.created_at)}</td>
                <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {r.user_id && <button type="button" className="btn primary" onClick={() => resetPw(r.user_id, r.username)}>임시 비밀번호 만들기</button>}{' '}
                  <button type="button" className="btn" onClick={() => dismiss(r.id)}>닫기</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        </Section>
      )}
      <Section title="회사" right={<button type="button" className="btn" onClick={() => editCompany()}>＋ 회사 만들기</button>}
        help="회사마다 허용 계정 수와 쓸 수 있는 용도(프로필)를 정합니다. 용도를 비우면 전부 쓸 수 있습니다.">
        {companies.length === 0 ? <div className="muted small" style={{ marginTop: 8 }}>아직 회사가 없습니다.</div> : (
          <table className="set-table">
            <thead><tr><th>회사</th><th>계정 (발급/허용)</th><th>쓸 수 있는 용도</th><th>메모</th><th /></tr></thead>
            <tbody>{companies.map((c) => (
              <tr key={c.id}>
                <td><b>{c.name}</b></td>
                <td>{c.issued} / {c.seats ?? '제한 없음'}</td>
                <td className="small">{c.profiles.length ? c.profiles.join(', ') : '전부'}</td>
                <td className="small muted">{c.note || '—'}</td>
                <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <button type="button" className="btn" onClick={() => editCompany(c)}>고치기</button>{' '}
                  <button type="button" className="btn danger" onClick={() => removeCompany(c)}>지우기</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Section>
      <Section title={`계정 ${users.length}개`} right={<>
        <select className="select" value={filter} onChange={(e) => setFilter(e.target.value)} title="회사로 거르기">
          <option value="">모든 회사</option>{companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button type="button" className="btn primary" onClick={issue}>＋ 계정 발급</button></>}>
        <table className="set-table">
          <thead><tr><th>아이디</th><th>이름</th><th>회사</th><th>역할</th><th>상태</th><th>마지막 로그인</th><th /></tr></thead>
          <tbody>{shown.map((u) => (
            <tr key={u.id} className={u.active ? '' : 'muted'}>
              <td><b>{u.username}</b>{u.id === me?.id && <span className="muted small"> (나)</span>}</td>
              <td>{u.display_name || '—'}</td>
              <td>{company(u.company_id) || '—'}</td>
              <td>{u.role === 'admin' ? <Badge kind="admin">관리자</Badge> : '사용자'}</td>
              <td>{!u.active ? <Badge kind="fail">사용 중지</Badge> : u.must_change ? <Badge kind="warn">임시 비밀번호</Badge>
                : u.password_expired ? <Badge kind="warn">비밀번호 {state.password_max_age_days}일 지남</Badge> : <Badge kind="ok">사용 중</Badge>}</td>
              <td className="small">{u.last_login_at ? fmtTime(u.last_login_at) : '—'}</td>
              <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                <button type="button" className="btn" onClick={() => resetPw(u.id, u.username)}>비밀번호 재설정</button>{' '}
                <button type="button" className="btn" onClick={() => editUser(u)}>고치기</button>{' '}
                {u.id !== me?.id && <><button type="button" className="btn" onClick={() => toggle(u)}>{u.active ? '사용 중지' : '다시 사용'}</button>{' '}
                  <button type="button" className="btn danger" onClick={() => remove(u)}>지우기</button></>}
              </td>
            </tr>
          ))}</tbody>
        </table>
      </Section>
    </>
  );
}
