// 관리 (관리자만): /admin/users 사용자·회사 · /admin/providers 외부 AI 모델 · /admin/records 기록·사용량 · /admin/access 접속 설정
// (하네스 — 지침 · 역할 · 자료 — 는 '설정 · 프롬프트' 에 있다: /settings/harness)
import { useEffect, useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { api } from '@/api';
import { useAuth } from '@/state/Auth';
import { useTitle } from '@/state/title';
import { AccessTab } from './AccessTab';
import { ProvidersTab } from './ProvidersTab';
import { RecordsTab } from './RecordsTab';
import { UsersTab } from './UsersTab';

const TABS: [string, string][] = [['users', '사용자 · 회사'], ['providers', '외부 AI 모델'], ['records', '기록 · 사용량'], ['access', '접속 설정']];

export function AdminView() {
  useTitle('관리');
  const { tab } = useParams();
  const navigate = useNavigate();
  const { isAdmin, state } = useAuth();
  const [pending, setPending] = useState(0);
  useEffect(() => {
    if (state.login_enabled && isAdmin) api.admin.resetRequests(true).then((r) => setPending(r.length)).catch(() => {});
  }, [tab, state.login_enabled, isAdmin]);
  if (!isAdmin) return <Navigate to="/" replace />;
  if (tab === 'harness') return <Navigate to="/settings/harness" replace />;   // 예전 주소
  if (!tab || !TABS.some(([id]) => id === tab)) return <Navigate to={`/admin/${state.login_enabled ? 'users' : 'access'}`} replace />;
  return (
    <section className="view active" id="view-admin">
      <div className="scroller">
        <div className="page admin-page">
          <div className="page-head"><div><h1>관리</h1>
            <div className="muted small">
              {state.login_enabled ? '로그인 사용 중 — 고객사별 계정, 외부 AI 모델 연결, 전체 기록, 접속 주소를 관리합니다.'
                : '지금은 혼자 쓰기(로그인 없음)입니다. 여러 사람이 계정으로 쓰려면 접속 설정에서 로그인 사용을 켜세요.'}</div></div></div>
          <div className="stabs"><div className="stab-group">
            {TABS.map(([id, label]) => (
              <button key={id} type="button" className={`stab${tab === id ? ' active' : ''}`} onClick={() => navigate(`/admin/${id}`)}>
                {label}{id === 'users' && pending > 0 && <span className="stab-count">{pending}</span>}
              </button>
            ))}
          </div></div>
          {tab === 'users' && <UsersTab onRequests={setPending} />}
          {tab === 'providers' && <ProvidersTab />}
          {tab === 'records' && <RecordsTab />}
          {tab === 'access' && <AccessTab />}
        </div>
      </div>
    </section>
  );
}
