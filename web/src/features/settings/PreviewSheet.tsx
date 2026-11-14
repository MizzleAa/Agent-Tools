// 미리보기 창 내용: 토큰 합계·예산 비율 · 점검 경고 · 조각별 내용 · 도구 설명 JSON
import type { Schemas } from '@/api';
import { CopyButton } from '@/ui/CopyButton';

export function PreviewBody({ r }: { r: Schemas['PromptPreview'] }) {
  const pct = Math.round((r.total_tokens / r.budget) * 100);
  const approx = r.exact ? '' : ' (추정 — 모델 서버가 켜져 있으면 정확히 셉니다)';
  const langs = Object.entries(r.lang_extra);
  return (
    <>
      <div className="pv-sum">
        <div><b>{r.total_tokens.toLocaleString()} 토큰</b> = 시스템 프롬프트 {r.system_tokens.toLocaleString()} + 도구 설명 {r.tools.tokens.toLocaleString()} ({r.tools.count}개){approx}</div>
        <div className="muted small">모델 {r.model} · 컨텍스트 {r.context_length.toLocaleString()} 중 답변 여유를 뺀 예산 {r.budget.toLocaleString()} 의 {pct}% — 나머지가 대화·도구 결과에 쓰입니다.</div>
        <div className="bar"><span style={{ width: `${Math.min(pct, 100)}%` }} /></div>
        {langs.length > 0 && <div className="muted small" style={{ marginTop: 6 }}>개발 언어를 알아채면 언어별 규격이 붙습니다: {langs.map(([k, v]) => `${k} +${v}`).join(' · ')} 토큰</div>}
      </div>
      {r.warnings.length ? (
        <div className="pv-warn">
          {r.warnings.map((w, i) => <div key={i} className={`pv-w ${w.level}`}><span className="pv-w-icon">{w.level === 'warn' ? '⚠' : 'ℹ'}</span><span>{w.text}</span></div>)}
        </div>
      ) : <div className="pv-w ok"><span className="pv-w-icon">✓</span><span>점검에서 걸린 것이 없습니다.</span></div>}
      <div className="pv-actions"><CopyButton text={r.prompt} label="시스템 프롬프트 복사" /></div>
      {r.sections.map((s, i) => (
        <details key={s.id} className="pv-sec" open>
          <summary><span className="brow-n">{i + 1}</span><b>{s.title}</b><span className="chip">{s.id}</span><span className="spacer" />
            <span className="muted small">{s.chars.toLocaleString()}자 · {s.tokens.toLocaleString()}토큰</span></summary>
          <pre className="block">{s.text}</pre>
        </details>
      ))}
      <details className="pv-sec">
        <summary><b>도구 설명 (모델이 보는 JSON)</b><span className="spacer" />
          <span className="muted small">{r.tools.chars.toLocaleString()}자 · {r.tools.tokens.toLocaleString()}토큰</span></summary>
        <pre className="block">{r.tools.json}</pre>
      </details>
    </>
  );
}
