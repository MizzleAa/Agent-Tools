// 대화 한 턴 = 실행 하나: 사용자 요청 + 에이전트가 한 일(생각·도구·승인·노드) + 최종 답
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type RunEvent } from '@/api';
import { fmtArgs, toolSummary } from '@/lib/format';
import { PERM, STATUS, TOOL_LABEL } from '@/lib/labels';
import { CopyButton } from '@/ui/CopyButton';
import { Icon, Spinner } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import { useUi } from '@/ui/UiProvider';
import { ImageGallery } from './ImageGallery';
import { buildTurn, finalNotice, type Item, type NodeItem, nodeOk, STUCK, type ToolItem, type TurnModel } from './turnModel';

export interface ContinueOptions { workflow?: string; resume?: boolean }

export function TurnView({ events, live, isLast, onContinue }: {
  events: RunEvent[]; live: boolean; isLast: boolean; onContinue: (text: string, extra?: ContinueOptions) => void;
}) {
  const turn = buildTurn(events);
  return (
    <>
      <div className="user-msg"><div className="user-bubble">{turn.task}</div></div>
      <div className="assistant">
        <Items items={turn.items} turn={turn} live={live} />
        {!turn.final && <ImageGallery runId={turn.runId} images={turn.images} />}
        {live && !turn.final && (
          <div className="muted small" style={{ display: 'flex', gap: 8, alignItems: 'center' }}><Spinner /><span>작업 중…</span></div>
        )}
        {turn.final && <FinalView turn={turn} isLast={isLast} onContinue={onContinue} />}
      </div>
    </>
  );
}

function Items({ items, turn, live }: { items: Item[]; turn: TurnModel; live: boolean }) {
  return (
    <>
      {items.map((it) => {
        switch (it.kind) {
          case 'thinking':
            return (
              <details key={it.key} className="thinking">
                <summary><Icon name="chevron" size={14} className="chev" />생각 과정</summary>
                <div className="thinking-body">{it.text}</div>
              </details>
            );
          case 'interim': return <div key={it.key} className="interim"><Markdown text={it.text} /></div>;
          case 'tool': return <ToolView key={it.key} item={it} runId={turn.runId} live={live} />;
          case 'note': return <div key={it.key} className="loop-note" title={it.injected}>↺ {it.message}</div>;
          case 'error': return <div key={it.key} className="notice error"><span className="msg">{it.message}</span></div>;
          case 'strip': return <WorkflowStrip key={it.key} turn={turn} />;
          case 'node': return <NodeView key={it.key} node={it} turn={turn} live={live} />;
        }
      })}
    </>
  );
}

function ToolView({ item, runId, live }: { item: ToolItem; runId: string; live: boolean }) {
  const { call, approval, approved, result } = item;
  const waiting = !!approval && approved === undefined;
  const state = result ? (result.ok ? 'ok' : 'fail') : waiting ? 'wait' : '';
  return (
    <details className="tool" open={waiting || undefined}>
      <summary>
        <span className={`tool-icon ${state}`}>
          {result ? <Icon name={result.ok ? 'check' : 'x'} size={15} /> : waiting ? <Icon name="hand" size={15} /> : <Spinner />}
        </span>
        <span className="tool-label">{TOOL_LABEL[call.name] ?? call.name}</span>
        <span className="tool-sum">{toolSummary(call.name, call.arguments)}</span>
        <span className="tool-meta" title={`권한: ${PERM[call.permission] ?? call.permission}`}>{result ? `${result.seconds.toFixed(1)}s` : ''}</span>
        <Icon name="chevron" size={14} className="chev" />
      </summary>
      <div className="tool-body">
        <div className="lbl">입력</div>
        <pre className="block">{fmtArgs(call.arguments)}</pre>
        {approval && <ApprovalPanel runId={runId} callId={call.call_id} reason={approval.reason} always={!!approval.always_ask} approved={approved} live={live} />}
        {result && <><div className="lbl">결과</div><pre className="block">{result.output}</pre></>}
      </div>
    </details>
  );
}

function ApprovalPanel({ runId, callId, reason, always, approved, live }: {
  runId: string; callId: string; reason: string; always: boolean; approved?: boolean; live: boolean;
}) {
  const ui = useUi();
  const [busy, setBusy] = useState(false);
  if (approved !== undefined) {
    return <div className="approval done"><span className="reason">{approved ? '✔ 승인됨' : '✖ 거부됨'}</span></div>;
  }
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); } catch (e) { ui.error(e); setBusy(false); }
  };
  return (
    <div className="approval">
      <span className="reason">승인이 필요합니다 — {reason}</span>
      {always && <span className="always">이 항목은 '모두 승인' 중에도 매번 직접 확인합니다.</span>}
      {live && <>
        {!always && (
          <button type="button" className="btn" disabled={busy} title="이번 실행이 끝날 때까지 더 묻지 않음"
            onClick={() => act(() => api.runs.autoApprove(runId, 'all'))}>이번 실행은 모두 승인</button>
        )}
        <button type="button" className="btn danger" disabled={busy} onClick={() => act(() => api.runs.approve(runId, callId, false))}>거부</button>
        <button type="button" className="btn primary" disabled={busy} onClick={() => act(() => api.runs.approve(runId, callId, true))}>승인</button>
      </>}
    </div>
  );
}

function WorkflowStrip({ turn }: { turn: TurnModel }) {
  const wf = turn.workflow!;
  return (
    <div className="wf-strip">
      <span className="wf-name">⛓ {wf.name}</span>
      {wf.nodes.map((n, i) => (
        <span key={n.id} style={{ display: 'contents' }}>
          {i > 0 && <span className="wf-arrow">→</span>}
          <span className={`wf-chip ${({ running: 'running', ok: 'ok', fail: 'fail', skip: 'skip' } as Record<string, string>)[wf.state[n.id]] ?? ''}`}
            title={`${n.profile}${n.after.length ? ` · 기다림: ${n.after.join(', ')}` : ''}`}>
            <span className="wf-n">{i + 1}</span>{n.title}
          </span>
        </span>
      ))}
    </div>
  );
}

function NodeView({ node, turn, live }: { node: NodeItem; turn: TurnModel; live: boolean }) {
  const { start, finish } = node;
  const ok = finish ? nodeOk(finish.status) : false;
  const title = start?.title ?? finish?.title ?? '';
  const meta = finish ? [STATUS[finish.status] ?? finish.status, finish.steps ? `스텝 ${finish.steps}` : '',
    finish.seconds ? `${finish.seconds.toFixed(0)}s` : ''].filter(Boolean).join(' · ') : '';
  return (
    <details className="wf-node" open={!finish || !ok || undefined}>
      <summary>
        <span className={`tool-icon ${finish ? (ok ? 'ok' : finish.status === 'skipped' ? '' : 'fail') : ''}`}>
          {finish ? <Icon name={ok ? 'check' : finish.status === 'skipped' ? 'chevron' : 'x'} size={15} /> : <Spinner />}
        </span>
        {start && <span className="wf-n">{start.index}</span>}
        <b>{title}</b>
        {start && <span className="chip">{start.profile}</span>}
        <span className="spacer" />
        <span className="tool-meta">{meta}</span>
        <Icon name="chevron" size={14} className="chev" />
      </summary>
      <div className="wf-body">
        {start && (
          <details className="wf-task">
            <summary className="muted small">이 노드에 준 지시</summary>
            <pre className="block">{start.task}</pre>
          </details>
        )}
        <Items items={node.items} turn={turn} live={live} />
        {finish && <><div className="lbl">노드 결과</div><Markdown className="answer wf-answer" text={finish.answer || '(빈 답변)'} /></>}
      </div>
    </details>
  );
}

function FinalView({ turn, isLast, onContinue }: { turn: TurnModel; isLast: boolean; onContinue: (t: string, e?: ContinueOptions) => void }) {
  const ui = useUi();
  const f = turn.final!;
  const folder = turn.workdir ? turn.workdir.split(/[\\/]/).pop() : '';
  const wf = turn.workflow;
  const canContinue = isLast && f.status !== 'done' && (STUCK.includes(f.status) || !!wf);
  return (
    <>
      <Markdown className="answer" text={f.answer || '(빈 답변)'} />
      <ImageGallery runId={turn.runId} images={turn.images} />
      <div className="answer-foot">
        <CopyButton text={f.answer} />
        {folder && (
          <button type="button" className="mini-btn" title={`${turn.workdir}\n작업 파일과 작업기록.md 가 있는 폴더를 엽니다`}
            onClick={() => api.runs.openFolder(turn.runId).catch(ui.error)}>
            <Icon name="folder" size={14} /><span>{folder}</span>
          </button>
        )}
        {folder && <Link className="mini-btn" to={`/files/${encodeURIComponent(folder)}`} title="이 작업 폴더의 파일과 코드를 화면에서 봅니다">코드 보기</Link>}
        <span className="meta">스텝 {f.steps} · 토큰 {(f.prompt_tokens + f.completion_tokens).toLocaleString()} · {f.seconds.toFixed(1)}s</span>
      </div>
      {f.status !== 'done' && (
        <div className={`notice ${f.status === 'error' ? 'error' : ''}`}>
          <span className="msg">{finalNotice(f.status, f.steps, !!wf)}</span>
          {canContinue && (
            <button type="button" className="btn" onClick={() => (wf && f.status !== 'partial'
              ? onContinue('멈춘 노드부터 계속', { workflow: wf.name, resume: true })   // 성공한 노드는 다시 돌리지 않는다
              : onContinue('멈춘 곳부터 계속 진행해줘.'))}>
              {wf ? '멈춘 노드부터 계속' : '계속하기'}
            </button>
          )}
        </div>
      )}
    </>
  );
}
