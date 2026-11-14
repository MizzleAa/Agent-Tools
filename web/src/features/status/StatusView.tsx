// 상태 · 연결: 모델 서버, 모델, GPU, 작업 폴더·한도, 사내 API, 사내 사이트, 읽기 허용 내부 주소
import { api } from '@/api';
import { useApp } from '@/state/AppState';
import { useTitle } from '@/state/title';
import { Badge, Chip } from '@/ui/form';
import { RUNTIME } from '@/lib/labels';
import { ApiCard, HostsCard, SharesCard, SitesCard } from './ConnectionCards';
import { ModelsCard } from './ModelsCard';
import { Card, KV, useAction } from './parts';

export function StatusView() {
  useTitle('상태 · 연결');
  const { status: s, refreshStatus } = useApp();
  const act = useAction();
  if (!s) return <section className="view active"><div className="scroller"><div className="page"><div className="muted">불러오는 중…</div></div></div></section>;
  const rt = s.runtime;
  const problems = [...(rt.problems ?? []), rt.error].filter(Boolean);
  const gpuPct = s.gpu ? Math.round((s.gpu.used_mb / s.gpu.total_mb) * 100) : 0;
  return (
    <section className="view active">
      <div className="scroller">
        <div className="page">
          <div className="page-head">
            <h1>상태 · 연결</h1>
            <button type="button" className="btn" onClick={refreshStatus}>새로고침</button>
          </div>
          <div className="cards">
            <Card title="모델 서버" right={<Badge kind={rt.state === 'ok' ? 'ok' : rt.state === 'loading' ? 'warn' : 'fail'}>{RUNTIME[rt.state]}</Badge>}>
              {rt.notice && <div className="muted small" style={{ marginTop: 8 }}>알림: {rt.notice}</div>}
              <KV rows={[
                ['주소', s.backend.url], ['모델 이름', s.backend.model],
                ['모델 파일', rt.model_size_gb ? `${rt.model_file} (${rt.model_size_gb} GB)` : `${rt.model_file} (없음)`],
                ['컨텍스트', `${(rt.n_ctx ?? s.backend.context_length).toLocaleString()} 토큰`], ['빌드', String(rt.build ?? '-')],
              ]} />
              {rt.stage && <div className="muted small" style={{ marginTop: 8 }}>진행 중: {rt.stage}</div>}
              {problems.length > 0 && <div className="notice error" style={{ marginTop: 10 }}><span className="msg">{problems.join('\n')}</span></div>}
              {rt.log_tail && (
                <details open style={{ marginTop: 8 }}><summary className="small muted">서버 로그 (마지막 부분)</summary><pre className="block">{rt.log_tail}</pre></details>
              )}
              <div className="form-row" style={{ justifyContent: 'flex-end' }}>
                {rt.state === 'down' && <button type="button" className="btn primary" onClick={() => act(api.system.startRuntime)}>모델 서버 시작</button>}
                {rt.managed && <button type="button" className="btn danger" onClick={() => act(api.system.stopRuntime)}>모델 서버 중지</button>}
              </div>
            </Card>
            {s.models.length > 0 && <ModelsCard models={s.models} />}
            {s.gpu && (
              <Card title="GPU" right={<Chip>사용률 {s.gpu.util}%</Chip>}>
                <KV rows={[['이름', s.gpu.name], ['VRAM', `${(s.gpu.used_mb / 1024).toFixed(1)} / ${(s.gpu.total_mb / 1024).toFixed(1)} GB (${gpuPct}%)`]]} />
                <div className="bar"><span style={{ width: `${gpuPct}%` }} /></div>
              </Card>
            )}
            <Card title="작업 폴더와 권한">
              <KV rows={[['작업 폴더', s.workspace], ...s.allowed_dirs.map((d): [string, string] => [d.mode === 'rw' ? '읽기/쓰기' : '읽기 전용', d.path])]} />
              <div className="muted small" style={{ marginTop: 8 }}>config/settings.yaml 의 allowed_dirs 에서 바꿀 수 있습니다.</div>
            </Card>
            <Card title="한도">
              <KV rows={[
                ['최대 스텝 (기본)', String(s.limits.max_steps || '무제한')],
                ['최대 실행 시간', s.limits.max_seconds ? `${s.limits.max_seconds}s` : '무제한'],
                ['도구 출력 최대', `${s.limits.tool_output_chars}자`], ['명령 실행 최대', `${s.limits.command_timeout}s`],
                ['웹 검색', s.web.provider === 'none' ? '꺼짐 (폐쇄망)' : `${s.web.provider} · ${s.web.regions.join(' + ')}`],
              ]} />
              <div className="muted small" style={{ marginTop: 8 }}>설정 · 프롬프트 › 실행 파라미터에서 바꿀 수 있습니다.</div>
            </Card>
            <ApiCard apis={s.intranet.apis} />
            <SitesCard sources={s.intranet.sources} />
            <HostsCard hosts={s.intranet.allowed_hosts} />
            <SharesCard />
          </div>
        </div>
      </div>
    </section>
  );
}
