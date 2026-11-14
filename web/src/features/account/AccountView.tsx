// 내 계정 (로그인 사용 모드): 내 정보 · 비밀번호 바꾸기 · 내 사내 API 계정 (각자 자기 계정으로 조회 — 값은 다시 보이지 않는다)
import { useCallback, useEffect, useState } from 'react';
import { api, type Schemas } from '@/api';
import { fmtTime } from '@/lib/format';
import { useAuth } from '@/state/Auth';
import { useTitle } from '@/state/title';
import { Badge, Section } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';
import { ChangePasswordForm } from '@/features/auth/ChangePasswordForm';

export function AccountView() {
  useTitle('내 계정');
  const { user, setState, state } = useAuth();
  return (
    <section className="view active">
      <div className="scroller">
        <div className="page">
          <div className="page-head"><div><h1>내 계정</h1>
            <div className="muted small">비밀번호와, 각자 자기 계정으로 조회하는 사내 API 계정을 관리합니다.</div></div></div>
          {!user ? (
            <Section title="혼자 쓰기 (로그인 없음)">
              <div className="muted small" style={{ marginTop: 8 }}>
                지금은 이 PC 에서 로그인 없이 쓰는 모드입니다. 여러 사람이 계정으로 쓰려면 '관리 › 접속 설정'에서 로그인 사용을 켜세요.
                사내 API 계정은 '상태 · 연결' › 사내 API 의 인증 칸에 넣습니다.</div>
            </Section>
          ) : (
            <>
              <Section title="내 정보">
                <dl className="kv">
                  <dt>아이디</dt><dd>{user.username}</dd>
                  <dt>이름</dt><dd>{user.display_name || '—'}</dd>
                  <dt>회사</dt><dd>{user.company_name || '—'}</dd>
                  <dt>역할</dt><dd>{user.role === 'admin' ? '관리자' : '사용자'}</dd>
                  <dt>비밀번호 바꾼 때</dt><dd>{fmtTime(user.password_changed_at)}
                    {user.password_expired && <> <Badge kind="warn">{state.password_max_age_days}일 지남</Badge></>}</dd>
                </dl>
              </Section>
              <Section title="비밀번호 바꾸기" help="바꾸면 다른 PC·브라우저에 열려 있던 로그인은 끝납니다.">
                <div style={{ maxWidth: 360 }}><ChangePasswordForm onDone={setState} /></div>
              </Section>
              <MyApiAccounts />
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function MyApiAccounts() {
  const ui = useUi();
  const [items, setItems] = useState<Schemas['MyApiAccount'][] | null>(null);
  const load = useCallback(async () => { try { setItems(await api.me.apiAccounts()); } catch (e) { ui.error(e); } }, [ui]);
  useEffect(() => { load(); }, [load]);
  const enter = async (a: Schemas['MyApiAccount']) => {
    const v = await ui.form({
      title: `'${a.title}' 내 계정`, confirmText: '저장',
      message: '이 사내 API 는 각자 자기 계정으로 조회합니다. 넣은 값은 이 서버의 비밀 저장소에만 두고, 화면·채팅·모델·기록에 나오지 않습니다.',
      fields: [{ name: 'username', label: '아이디' }, { name: 'password', label: '비밀번호', type: 'password', placeholder: '(다시 보이지 않습니다)' }],
    });
    if (!v) return;
    try {
      await api.me.setApiAccount(a.name, { username: v.username, password: v.password });
      const r = await api.me.checkApiAccount(a.name);
      ui.toast(r.message, r.ok ? 'ok' : 'error', 6000);
    } catch (e) { ui.error(e); }
    load();
  };
  const clear = async (a: Schemas['MyApiAccount']) => {
    if (!(await ui.confirm({ title: `'${a.title}' 내 계정을 지울까요?`, danger: true, confirmText: '지우기' }))) return;
    try { await api.me.clearApiAccount(a.name); } catch (e) { ui.error(e); }
    load();
  };
  const check = async (a: Schemas['MyApiAccount']) => {
    try { const r = await api.me.checkApiAccount(a.name); ui.toast(r.message, r.ok ? 'ok' : 'error', 6000); } catch (e) { ui.error(e); }
  };
  return (
    <Section title="내 사내 API 계정" help="에이전트가 이 API 를 조회할 때 내 계정(내 권한)으로 로그인합니다. 계정이 없으면 다른 사람의 계정으로 대신 조회하지 않습니다.">
      {!items ? <div className="muted small">불러오는 중…</div> : items.length === 0 ? (
        <div className="muted small" style={{ marginTop: 8 }}>각자 로그인하는 사내 API 가 아직 없습니다 (관리자가 등록합니다).</div>
      ) : (
        <table className="set-table">
          <thead><tr><th>API</th><th>주소</th><th>내 계정</th><th /></tr></thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.name}>
                <td>{a.title}</td>
                <td className="muted small">{a.base_url}</td>
                <td>{a.has_account ? <Badge kind="ok">저장됨</Badge> : <Badge kind="warn">없음</Badge>}</td>
                <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <button type="button" className="btn" onClick={() => enter(a)}>{a.has_account ? '바꾸기' : '넣기'}</button>{' '}
                  {a.has_account && <><button type="button" className="btn" onClick={() => check(a)}>로그인 확인</button>{' '}
                    <button type="button" className="btn danger" onClick={() => clear(a)}>지우기</button></>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
