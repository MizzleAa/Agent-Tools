// 승인 요청 · 질문 · 계획 승인 (입력창 위). 승인 절차를 건너뛰는 기능은 없다 — 사람이 직접 고른다.
import { useState } from 'react';
import { Markdown } from '@/ui/Markdown';
import { resolvePermission } from './actions';
import { shortPath, suggestionLabel, TOOL_LABEL, toolSummary } from './lib';
import { ToolInput } from './ToolCard';
import type { PermissionReq } from './types';

type Q = { question: string; header?: string; multiSelect?: boolean; options?: { label: string; description?: string }[] };

function Question({ req, sessionId }: { req: PermissionReq; sessionId: string }) {
  const qs = (req.input.questions as Q[] | undefined) ?? [];
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const ready = qs.every((q) => (picked[q.question]?.length ?? 0) > 0 || (other[q.question] ?? '').trim());
  const toggle = (q: Q, label: string) => setPicked((p) => {
    const cur = p[q.question] ?? [];
    return { ...p, [q.question]: q.multiSelect ? (cur.includes(label) ? cur.filter((x) => x !== label) : [...cur, label]) : [label] };
  });
  const submit = async () => {
    setBusy(true);
    const answers: Record<string, string> = {};
    for (const q of qs) answers[q.question] = [...(picked[q.question] ?? []), ...((other[q.question] ?? '').trim() ? [other[q.question].trim()] : [])].join(', ');
    await resolvePermission(sessionId, req.id, { behavior: 'allow', answers });
  };
  return (
    <>
      {qs.map((q) => (
        <div key={q.question} className="cc-question">
          <b>{q.header && <span className="chip">{q.header}</span>} {q.question}</b>{q.multiSelect && <span className="muted small"> (여러 개)</span>}
          <div className="cc-options">
            {(q.options ?? []).map((o) => (
              <button key={o.label} type="button" className={`cc-option${picked[q.question]?.includes(o.label) ? ' on' : ''}`} onClick={() => toggle(q, o.label)}>
                <b>{o.label}</b>{o.description && <span className="muted small">{o.description}</span>}
              </button>
            ))}
          </div>
          <input type="text" placeholder="직접 입력 (선택)" value={other[q.question] ?? ''} onChange={(e) => setOther((x) => ({ ...x, [q.question]: e.target.value }))} />
        </div>
      ))}
      <div className="cc-perm-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => resolvePermission(sessionId, req.id, { behavior: 'deny', message: '사용자가 답하지 않고 넘어갔습니다.' })}>건너뛰기</button>
        <button type="button" className="btn primary" disabled={!ready || busy} onClick={submit}>답변 보내기</button>
      </div>
    </>
  );
}

function Permission({ req, sessionId, cwd }: { req: PermissionReq; sessionId: string; cwd: string }) {
  const [reason, setReason] = useState('');
  const [denying, setDenying] = useState(false);
  const [busy, setBusy] = useState(false);
  const plan = req.kind === 'plan';
  const act = async (body: Parameters<typeof resolvePermission>[2]) => { setBusy(true); await resolvePermission(sessionId, req.id, body); };
  return (
    <>
      <div className="cc-perm-detail">
        {plan ? <div className="cc-plan"><Markdown text={String(req.input.plan ?? '')} /></div>
          : <ToolInput use={{ type: 'tool_use', id: req.tool_use_id ?? req.id, name: req.tool_name, input: req.input }} />}
      </div>
      {(req.decision_reason || req.blocked_path) && (
        <div className="muted small">{req.decision_reason}{req.blocked_path && <>{req.decision_reason ? ' · ' : ''}대상: <code className="inline">{shortPath(req.blocked_path, cwd)}</code></>}</div>
      )}
      {denying && (
        <div className="inline-add">
          <input type="text" autoFocus value={reason} placeholder={plan ? '계획을 어떻게 바꿀지 알려 주세요' : '거부 사유 (Claude 에게 전달, 선택)'}
            onChange={(e) => setReason(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') act({ behavior: 'deny', message: reason || undefined }); }} />
          <button type="button" className="btn danger" disabled={busy} onClick={() => act({ behavior: 'deny', message: reason || undefined })}>{plan ? '다시 계획' : '거부'}</button>
          <button type="button" className="btn" disabled={busy} onClick={() => act({ behavior: 'deny', message: reason || '사용자가 거부하고 중단했습니다.', interrupt: true })}>거부하고 중단</button>
        </div>
      )}
      <div className="cc-perm-actions">
        {req.suggestions.map((sg, i) => (
          <button key={i} type="button" className="btn" disabled={busy} title={suggestionLabel(sg)} onClick={() => act({ behavior: 'allow', apply_suggestions: [i] })}>
            {plan ? '승인 + ' : '승인 + '}{suggestionLabel(sg, cwd)}
          </button>
        ))}
        {!denying && <button type="button" className="btn danger" disabled={busy} onClick={() => setDenying(true)}>{plan ? '수정 요청' : '거부'}</button>}
        <button type="button" className="btn primary" disabled={busy} onClick={() => act({ behavior: 'allow' })}>{plan ? '계획 승인' : '승인'}</button>
      </div>
    </>
  );
}

export function PermissionPanel({ pending, sessionId, cwd }: { pending: PermissionReq[]; sessionId: string; cwd: string }) {
  const req = pending[0];
  if (!req) return null;
  const title = req.kind === 'question' ? 'Claude 가 질문했습니다' : req.kind === 'plan' ? '계획을 검토하고 승인해 주세요'
    : `승인이 필요합니다 — ${TOOL_LABEL[req.tool_name] ?? req.tool_name}`;
  return (
    <div className="approval cc-approval" role="alertdialog" aria-label={title}>
      <span className="reason">{title}{req.kind === 'permission' && <code className="inline cc-perm-sum">{toolSummary(req.tool_name, req.input, cwd)}</code>}
        {pending.length > 1 && <span className="chip">+{pending.length - 1}개 대기</span>}</span>
      <div className="cc-perm-body">
        {req.kind === 'question' ? <Question key={req.id} req={req} sessionId={sessionId} /> : <Permission key={req.id} req={req} sessionId={sessionId} cwd={cwd} />}
      </div>
    </div>
  );
}
