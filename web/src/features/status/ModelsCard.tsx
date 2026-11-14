import { Link } from 'react-router-dom';
import { api, type Schemas } from '@/api';
import { useApp } from '@/state/AppState';
import { Badge, Chip } from '@/ui/form';
import { useUi } from '@/ui/UiProvider';

export function ModelsCard({ models }: { models: Schemas['ModelInfo'][] }) {
  const ui = useUi();
  const { status, refreshStatus } = useApp();
  const use = async (name: string) => {
    if (status?.active_run) { ui.toast('실행 중인 작업이 끝난 뒤에 모델을 바꿀 수 있습니다.'); return; }
    if (!(await ui.confirm({ title: `'${name}' 모델로 바꿀까요?`, message: '모델 서버를 다시 띄우느라 수십 초 걸립니다. 그동안은 메시지를 보낼 수 없어요.', confirmText: '바꾸기' }))) return;
    try { await api.system.selectModel(name); } catch (e) { ui.error(e); }
    refreshStatus();
  };
  return (
    <div className="card wide">
      <div className="card-head"><span className="title">모델</span></div>
      <div className="muted small" style={{ marginTop: 4 }}>
        입력창 왼쪽 아래에서도 바꿀 수 있습니다. 모델 추가·모델별 값은 <Link to="/settings/models">설정 › 모델</Link> 에서.
      </div>
      {models.map((m) => (
        <div key={m.name} className="item">
          <div className="card-head">
            <span className="title">{m.name}</span><span className="spacer" />
            {m.size_gb && <Chip>{m.size_gb} GB</Chip>}
            {m.active ? <Badge kind="ok">사용 중</Badge>
              : m.state === 'ready' ? <button type="button" className="btn" onClick={() => use(m.name)}>이 모델 쓰기</button>
                : <Badge kind="warn">{m.state === 'downloading' ? '내려받는 중' : '파일 없음'}</Badge>}
          </div>
          <div className="muted small">{m.note}</div>
          {m.state === 'missing' && <div className="muted small">필요한 파일: {m.file}</div>}
        </div>
      ))}
    </div>
  );
}
