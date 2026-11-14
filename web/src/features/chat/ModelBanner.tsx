// 모델이 아직 준비되지 않았을 때 입력창 위에 뜨는 안내 (적재 중 · 꺼짐 · 포트 충돌 · 관리 서버 연결 끊김 · 외부 모델 연결 문제)
// 모델 서버 켜기·끄기는 관리자만 할 수 있으므로, 일반 사용자에게는 '관리자에게 알리세요'와 외부 모델로 바꾸는 길만 보인다.
import { useNavigate } from 'react-router-dom';
import { api } from '@/api';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { Spinner } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';

export function ModelBanner({ flash }: { flash: number }) {
  const { status, unreachable, modelReady, refreshStatus, external, setModelChoice } = useApp();
  const { isAdmin } = useAuth();
  const ui = useUi();
  const navigate = useNavigate();
  if (modelReady || (!status && !unreachable)) return null;
  const cls = `model-banner${flash ? ' flash' : ''}`;
  if (unreachable || !status) {
    return (
      <div key={flash} className={`${cls} down`}>
        <span className="msg">관리 서버에 연결할 수 없습니다.
          <span className="sub">start.cmd 창이 켜져 있는지 확인하세요. 다시 켜지면 자동으로 이어집니다.</span></span>
      </div>
    );
  }
  if (external) {   // 고른 외부 모델에 문제가 있다 (키 없음 · 연결 확인 실패)
    return (
      <div key={flash} className={`${cls} down`}>
        <span className="msg">외부 모델 '{external.connection_title} · {external.title}' 을 지금 쓸 수 없습니다
          <span className="sub">{external.problem || '키가 없거나 연결 확인이 실패했습니다.'}{isAdmin ? '' : ' 관리자에게 알리세요.'}</span></span>
        {isAdmin && <button type="button" className="btn" onClick={() => navigate('/admin/providers')}>외부 모델 관리</button>}
        <button type="button" className="btn primary" onClick={() => setModelChoice('')}>로컬 모델로 바꾸기</button>
      </div>
    );
  }
  const others = (status.external_models ?? []).filter((m) => m.ready);
  const rt = status.runtime;
  if (rt.state === 'loading') {
    return (
      <div key={flash} className={cls}>
        <Spinner />
        <span className="msg">모델을 불러오는 중입니다 · {status.backend.model}
          <span className="sub">{rt.stage ? `${rt.stage} — 보통 수십 초 걸립니다.` : '보통 수십 초 걸립니다. 준비되면 바로 보낼 수 있어요.'}</span></span>
      </div>
    );
  }
  const occupied = rt.state === 'occupied';
  const problems = [...(rt.problems ?? []), rt.error].filter(Boolean);
  return (
    <div key={flash} className={`${cls} down`}>
      <span className="msg">
        {occupied ? `모델 서버 포트(${rt.port})를 다른 프로그램이 쓰고 있습니다` : `모델 서버가 꺼져 있습니다 · ${status.backend.model}`}
        <span className="sub">{occupied ? "'다시 시작'을 누르면 빈 포트를 찾아 모델 서버를 띄웁니다. (Docker 등이 같은 포트를 쓰는 경우)"
          : problems.length ? problems.join(' / ') : isAdmin ? '다시 시작하거나 상태 · 연결에서 로그를 확인하세요.' : '관리자가 모델 서버를 켜야 합니다. 관리자에게 알리세요.'}</span>
      </span>
      {others.length > 0 && <button type="button" className="btn" title={others[0].connection_title}
        onClick={() => setModelChoice(others[0].key)}>☁ {others[0].title} 로 바꾸기</button>}
      {isAdmin && <>
        <button type="button" className="btn" onClick={() => navigate('/status')}>상태 보기</button>
        <button type="button" className="btn primary" onClick={async () => {
          try { await api.system.startRuntime(); } catch (e) { ui.error(e); }
          refreshStatus();
        }}>다시 시작</button>
      </>}
    </div>
  );
}
