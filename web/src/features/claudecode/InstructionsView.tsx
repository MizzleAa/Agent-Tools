// CLI 에 붙는 지침 보기 — 로컬·API 모델과 같은 '설정 · 프롬프트'(용도·블록)와 하네스에서 조립한 글.
import { useEffect, useState } from 'react';
import { Spinner } from '@/ui/Icon';
import { ccApi } from './api';
import { errText } from './actions';

type Ins = Awaited<ReturnType<typeof ccApi.instructions>>;

export function InstructionsView({ profile, sessionId, harness }: { profile?: string; sessionId?: string; harness?: string }) {
  const [ins, setIns] = useState<Ins | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    setIns(null);
    ccApi.instructions(profile, sessionId, harness).then(setIns).catch((e) => setErr(errText(e)));
  }, [profile, sessionId, harness]);
  if (err) return <div className="notice error"><span className="msg">{err}</span></div>;
  if (!ins) return <div className="muted"><Spinner /> 지침을 조립하는 중…</div>;
  return (
    <div className="cc-ins">
      <div className="muted small">
        용도 <b>{ins.profile}</b>{ins.harness && <> · 하네스 <b>{ins.harness}</b></>} — 로컬·API 모델과 같은 지침(공통 하네스{ins.harness ? ' + 고른 하네스' : ''})을 CLI 기본 지침 뒤에 덧붙입니다.
        {/* 라우터 밖(useUi 의 sheet)에서도 그려지므로 Link 가 아닌 일반 링크 */}
        {' '}바꾸려면 <a href="/settings">설정 · 프롬프트</a> (용도 · 블록 · 하네스). 대화의 첫 요청 때 붙은 지침은 그 대화 동안 그대로 쓰입니다.
      </div>
      {ins.sections.length === 0 && <div className="muted small">붙는 지침이 없습니다.</div>}
      {ins.sections.map((s) => (
        <details key={s.id} className="cc-ins-sec" open={ins.sections.length <= 3}>
          <summary><b>{s.title}</b> <span className="muted small">· {s.id} · {s.text.length.toLocaleString()}자</span></summary>
          <pre className="block">{s.text}</pre>
        </details>
      ))}
      {ins.roles.length > 0 && (
        <div className="small">하네스 용도 → CLI 서브에이전트: {ins.roles.map((r) => <span key={r.name} className="chip" title={r.description}>{r.name}</span>)}</div>
      )}
    </div>
  );
}
