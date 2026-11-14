// 대화 화면. 주소 /  = 새 작업, /c/<첫 실행 id> = 그 대화 (이어서 한 실행들을 차례로 보여 준다)
// 모델이 CLI · Claude Code 면 같은 자리에 CLI 대화 화면(features/claudecode)을 그린다: / = 새 대화, /cc/<세션 id> = 그 세션
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, type EventOf, type RunEvent, type Schemas } from '@/api';
import { firstLine, greeting } from '@/lib/format';
import { SUGGEST } from '@/lib/labels';
import { useStored } from '@/lib/storage';
import { groupThreads } from '@/lib/threads';
import { CcChatView } from '@/features/claudecode/CcChatView';
import { isCc } from '@/features/claudecode/lib';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { useTitle } from '@/state/title';
import { useUi } from '@/ui/UiProvider';
import { useWorkBase } from './BaseDirSelect';
import { Composer, type ComposerOptions } from './Composer';
import { ModelBanner } from './ModelBanner';
import type { SlashCommand } from './slash';
import { type ContinueOptions, TurnView } from './TurnView';

interface Turn { runId: string; events: RunEvent[]; live: boolean }

const finished = (t: Turn) => t.events.some((e) => e.type === 'Final');

export function ChatView() {
  const { rootId, ccId } = useParams();
  const { modelChoice } = useApp();
  const { isAdmin } = useAuth();
  if (isAdmin && (ccId || (!rootId && isCc(modelChoice)))) return <CcChatView key={ccId ?? 'new'} sessionId={ccId ?? null} />;
  return <AgentChatView />;
}

function AgentChatView() {
  const { rootId } = useParams();
  const navigate = useNavigate();
  const app = useApp();
  const { isAdmin } = useAuth();
  const ui = useUi();
  const [turns, setTurns] = useState<Turn[]>([]);
  const skipLoad = useRef<string | null>(null);
  const restored = useRef(false);
  const sources = useRef(new Map<string, EventSource>());
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const location = useLocation();
  // 파일 탐색기의 '이 파일로 작업 시키기': 새 작업 입력창에 경로를 채워 둔다 (보내기는 사용자가)
  const [text, setText] = useState(() => (location.state as { draft?: string } | null)?.draft ?? '');
  const [flash, setFlash] = useState(0);
  const [choice, setChoice] = useStored('profile', 'general');
  const [approve, setApprove] = useStored('approve-mode', 'none', ['none', 'writes', 'all']);
  const [maxSteps, setMaxSteps] = useStored('max-steps', '50', ['20', '50', '100', '0']);
  const [effort, setEffort] = useStored('effort', '', ['', 'low', 'medium', 'high', 'xhigh', 'max']);   // 외부 모델 추론 강도 ('' = 연결 기본)
  const [base, setBase] = useWorkBase();   // 작업 위치 (관리자 · 새 대화) — CLI 와 같은 값
  const [harness, setHarness] = useStored('harness', '');   // 회사·프로젝트 하네스 ('' = 공통만) — CLI 와 같은 값
  const options: ComposerOptions = { choice, approve, maxSteps, effort, base, harness };
  const setOption = (k: keyof ComposerOptions, v: string) =>
    ({ choice: setChoice, approve: setApprove, maxSteps: setMaxSteps, effort: setEffort, base: setBase, harness: setHarness })[k](v);
  const current = turns.find((t) => t.live && !finished(t));
  useTitle(turns[0] ? firstLine(turns[0].events.find((e) => e.type === 'RunStarted')?.task ?? '') : '');

  // ───────── 실시간 이벤트 (SSE)
  const follow = useCallback((runId: string) => {
    sources.current.get(runId)?.close();
    const source = new EventSource(api.runs.eventsUrl(runId));
    sources.current.set(runId, source);
    source.onmessage = (msg) => {
      const ev = JSON.parse(msg.data) as RunEvent;
      setTurns((ts) => ts.map((t) => (t.runId !== runId || t.events.some((x) => x.seq === ev.seq) ? t : { ...t, events: [...t.events, ev] })));
    };
    source.addEventListener('end', () => {
      source.close();
      sources.current.delete(runId);
      setTurns((ts) => ts.map((t) => (t.runId === runId ? { ...t, live: false } : t)));
      app.reloadThreads();
      app.refreshStatus();
    });
  }, [app]);

  useEffect(() => () => sources.current.forEach((s) => s.close()), []);   // 화면을 떠나면 보기만 멈춘다 (실행은 계속)

  // ───────── 주소가 바뀌면 그 대화를 불러온다
  useEffect(() => {
    if (rootId && skipLoad.current === rootId) { skipLoad.current = null; return; }   // 방금 여기서 시작한 대화 (이미 보여 주는 중)
    sources.current.forEach((s) => s.close());
    sources.current.clear();
    setTurns([]);
    if (!rootId) return;
    let stale = false;
    (async () => {
      // 내 최근 대화에 없으면 서버에 그 대화를 물어본다 (오래된 대화 · 관리자가 '기록'에서 남의 대화를 열 때)
      const thread = (app.threads.find((t) => t.root.id === rootId)
        ?? groupThreads(await api.runs.thread(rootId).catch(() => [])).find((t) => t.root.id === rootId));
      if (!thread) { if (!stale) ui.toast('대화를 찾을 수 없습니다.', 'error'); return; }
      const loaded: Turn[] = [];
      for (const r of thread.chain) {
        if (r.status === 'running') loaded.push({ runId: r.id, events: [], live: true });
        else loaded.push({ runId: r.id, events: (await api.runs.get(r.id)).events, live: false });
      }
      if (stale) return;
      setTurns(loaded);
      if (thread.last.profile) setChoice(thread.last.profile.startsWith('workflow:') ? `wf:${thread.last.profile.slice(9)}` : thread.last.profile);
      loaded.filter((t) => t.live).forEach((t) => follow(t.runId));
    })().catch((e) => ui.error(e));
    return () => { stale = true; };
  }, [rootId]);   // eslint-disable-line react-hooks/exhaustive-deps

  // 새로고침했을 때 실행 중인 작업이 있으면 그 대화로 돌아간다
  useEffect(() => {
    if (restored.current || rootId || !app.status) return;
    restored.current = true;
    const run = app.status.active_run;
    const t = run && app.threads.find((x) => x.chain.some((r) => r.id === run));
    if (t) navigate(`/c/${t.root.id}`);
  }, [app.status, app.threads, rootId, navigate]);

  // ───────── 스크롤: 맨 아래를 보고 있었으면 새 내용도 따라간다
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns]);
  const onScroll = () => {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 240;
  };

  // ───────── 보내기 · 중지
  const send = async (msg = text, extra: ContinueOptions = {}) => {
    if (current || !msg.trim()) return;
    if (!app.modelReady) { setFlash((f) => f + 1); return; }   // 안내가 있는 곳을 흔들어 알려 준다
    const body: Schemas['RunCreate'] = {
      task: msg.trim(), auto_approve: approve as Schemas['RunCreate']['auto_approve'], max_steps: Number(maxSteps),
      ...(choice.startsWith('wf:') ? { workflow: choice.slice(3) } : { profile: choice }), ...extra,
      ...(app.modelChoice ? { model: app.modelChoice } : {}),   // 외부 AI 모델을 골랐으면 그 연결로
      ...(effort ? { effort: effort as Schemas['RunCreate']['effort'] } : {}),   // 작업마다 고른 추론 강도 (로컬 · 외부 모두)
      ...(!rootId && base && isAdmin ? { base_dir: base } : {}),   // 새 대화: 고른 작업 위치 아래에 작업 폴더를 만든다
      ...(harness && app.harnesses.some((h) => h.id === harness) ? { harness } : {}),   // 고른 하네스 (지워졌으면 공통만)
    };
    const last = turns[turns.length - 1];
    if (rootId && last) body.parent_run_id = last.runId;   // 같은 대화면 이전 내용을 이어받는다
    try {
      const { run_id } = await api.runs.start(body);
      setText('');
      stick.current = true;
      setTurns((ts) => [...ts, { runId: run_id, events: [], live: true }]);
      follow(run_id);
      if (!rootId) { skipLoad.current = run_id; navigate(`/c/${run_id}`); }
      app.reloadThreads();
    } catch (e) {
      ui.error(e);
    }
  };
  const stop = async () => {
    if (current) await api.runs.cancel(current.runId).catch(ui.error);
  };

  // ───────── '/' 명령 중 이 대화를 보는 것 (나머지는 Composer: 선택 칸 · 하네스 명령 · /init · /review)
  const finals = turns.flatMap((t) => t.events.filter((e): e is EventOf<'Final'> => e.type === 'Final'));
  const lastCall = turns.flatMap((t) => t.events).filter((e): e is EventOf<'LLMCall'> => e.type === 'LLMCall').pop();
  const ctxLimit = app.external ? app.external.context_length : app.status?.runtime.n_ctx ?? null;
  const showUsage = () => {
    if (!finals.length) { ui.toast('아직 끝난 작업이 없습니다.'); return; }
    const sum = (k: 'steps' | 'prompt_tokens' | 'completion_tokens' | 'seconds') => finals.reduce((a, f) => a + f[k], 0);
    ui.sheet('이 대화의 사용량', (
      <div className="slash-sheet">
        <div><b>{finals.length}</b>번 요청 · 스텝 <b>{sum('steps')}</b> · 걸린 시간 <b>{Math.round(sum('seconds'))}</b>초</div>
        <div>입력 토큰 <b>{sum('prompt_tokens').toLocaleString()}</b> · 출력 토큰 <b>{sum('completion_tokens').toLocaleString()}</b></div>
        <div className="muted small">외부 모델은 이 토큰 수만큼 공급사 요금이 듭니다 (로컬 모델은 이 서버의 GPU 시간).</div>
      </div>
    ));
  };
  const showContext = () => {
    if (!lastCall) { ui.toast('아직 모델을 부르지 않았습니다 — 보내고 나면 대화가 얼마나 찼는지 보입니다.'); return; }
    const pct = ctxLimit ? Math.round((lastCall.prompt_tokens / ctxLimit) * 100) : null;
    ui.sheet('컨텍스트 사용량', (
      <div className="slash-sheet">
        <div>마지막 모델 호출의 입력 <b>{lastCall.prompt_tokens.toLocaleString()}</b> 토큰
          {ctxLimit ? <> / 모델 컨텍스트 <b>{ctxLimit.toLocaleString()}</b> (<b>{pct}%</b>)</> : null}</div>
        {pct !== null && <div className="bar"><span style={{ width: `${Math.min(pct, 100)}%` }} /></div>}
        <div className="muted small">지침 · 도구 설명 · 이 대화 내용 · 도구 결과가 모두 들어갑니다. 가득 차면 오래된 도구 결과 · 기록부터 줄여서 보냅니다 (지침 · 요청은 그대로) — 길어지면 /new 로 새 대화를 시작하세요.</div>
      </div>
    ));
  };
  const lastTask = turns.length ? turns[turns.length - 1].events.find((e): e is EventOf<'RunStarted'> => e.type === 'RunStarted')?.task : undefined;
  const chatCommands: SlashCommand[] = [
    { name: 'usage', aliases: ['사용량', 'cost'], desc: '이 대화의 사용량 (요청 · 스텝 · 토큰 · 시간)', kind: 'web', run: showUsage },
    { name: 'context', aliases: ['컨텍스트'], desc: '컨텍스트 사용량 (대화가 모델 한도에서 얼마나 찼는지)', kind: 'web', run: showContext },
    ...(lastTask && !current ? [{ name: 'retry', aliases: ['다시'], desc: '마지막 요청을 다시 보내기', kind: 'web' as const, run: () => void send(lastTask) }] : []),
    ...(current ? [{ name: 'stop', aliases: ['중지'], desc: '실행 중인 작업 중지', kind: 'web' as const, run: () => void stop() }] : []),
  ];

  const empty = turns.length === 0;
  return (
    <section className={`view active${empty ? ' empty' : ''}`} id="view-chat">
      <div className="scroller" ref={scroller} onScroll={onScroll}>
        {!empty && (
          <div className="thread">
            {turns.map((t, i) => (
              <TurnView key={t.runId} events={t.events} live={t.live} isLast={i === turns.length - 1} onContinue={(m, x) => send(m, x)} />
            ))}
          </div>
        )}
        {empty && <div className="welcome"><h1 className="greeting">{greeting()}</h1></div>}
      </div>
      <div className="composer-wrap">
        <ModelBanner flash={flash} />
        <Composer text={text} setText={setText} options={options} setOption={setOption} running={!!current}
          onSend={(m) => send(m)} onStop={stop} isNew={!rootId} extraCommands={chatCommands} />
        <div className="suggestions">
          {(SUGGEST[choice] ?? []).map((s) => <button key={s} type="button" className="suggestion" onClick={() => setText(s)}>{s}</button>)}
        </div>
      </div>
    </section>
  );
}
