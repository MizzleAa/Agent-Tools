// 대화 화면 — 모델이 CLI(Claude Code · Codex · Gemini)일 때 (주소 / 또는 /cc/<세션>). 같은 화면 틀(.thread · 입력창)을 쓴다.
// 엔진은 새 대화면 모델 선택값(cc: · cx: · gm:), 대화 중이면 그 세션의 engine.
// 새 대화: 보내면 에이전트의 새 작업처럼 작업 위치(기본 workspace) 아래에 '날짜_시각_요청' 폴더를 만들고 그 안에서 Claude Code 세션을 시작한다.
// 작업 위치는 로컬 에이전트와 같은 값(BaseDirSelect). 이어 하기: 같은 세션으로 (CLI 의 --resume).
import { type KeyboardEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { greeting } from '@/lib/format';
import { useStored } from '@/lib/storage';
import { useApp } from '@/state/AppState';
import { useTitle } from '@/state/title';
import { Icon, Spinner } from '@/ui/Icon';
import { BaseDirSelect, useWorkBase } from '@/features/chat/BaseDirSelect';
import { ccProblem, EFFORT_LABELS, harnessChoices, HarnessSelect, modelChoices, ModelSelect, pairChoices, roleChoices, roleGroups, useCcStatus,
  useModelChoice } from '@/features/chat/Composer';
import { cliCommands, harnessCommands, mergeCommands, type SlashCommand, templateCommands } from '@/features/chat/slash';
import { useSlash } from '@/features/chat/SlashMenu';
import { useUi } from '@/ui/UiProvider';
import { ccApi, type CliCommand } from './api';
import { InstructionsView } from './InstructionsView';
import { connectEvents, disconnectEvents, importExternal, interrupt, loadExternal, openSession, patchSession, sendTo, startAuto, startProcess,
  stopProcess, terminateOrphan } from './actions';
import { CcThread } from './CcThread';
import { billedPerToken, bytes, cliChoice, duration, ENGINE_LABEL, parseCli, permissionModes, PROCESS_LABEL, timeAgo, usageText } from './lib';
import { PermissionPanel } from './PermissionPanel';
import { chatOf, useCc } from './store';

export function CcChatView({ sessionId }: { sessionId: string | null }) {
  const navigate = useNavigate();
  const app = useApp();
  const st = useCcStatus();
  const session = useCc((s) => (sessionId ? s.sessions[sessionId] : undefined));
  const chat = useCc((s) => chatOf(s, sessionId));
  const projects = useCc((s) => s.projects);
  const proc = useCc((s) => (sessionId ? s.processes[sessionId] : undefined));
  const [base, setBase] = useWorkBase();   // 작업 위치 ('' = workspace) — 로컬 에이전트와 같은 값
  const [ccProfile, setCcProfile] = useStored('cc-profile', 'general');   // 용도 — 로컬·API 와 같은 '설정 · 프롬프트'·하네스 지침
  const [ccEffort, setCcEffort] = useStored('cc-effort', '', EFFORT_LABELS.map(([v]) => v));   // 추론 강도 (CLI --effort)
  const [harness, setHarness] = useStored('harness', '');   // 회사·프로젝트 하네스 ('' = 공통만) — 로컬 에이전트와 같은 값
  const pickedHarness = app.harnesses.some((h) => h.id === harness) ? harness : '';
  const { extra: harnessRoles, shared: sharedRoles } = roleGroups(app.profiles, app.harnesses, pickedHarness);
  const sessionHarness = session?.harness ? app.harnesses.find((h) => h.id === session.harness) : undefined;
  const ui = useUi();
  const [mode, setMode] = useStored('cc-permission-mode', 'default', ['default', 'acceptEdits', 'plan']);
  const [text, setText] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  const running = chat.turn !== null || Boolean(session?.busy);
  const suffix = '\\' + base.replace(/\//g, '\\').toLowerCase();   // 작업 위치(상대 경로)로 끝나는 프로젝트 = 그 위치 — Claude Code 기록 보기용
  const project = base ? projects.find((p) => p.path.toLowerCase().endsWith(suffix)) : undefined;
  const projectId = project?.id ?? '';
  const chosen = parseCli(app.modelChoice);
  const engine = session?.engine ?? chosen?.engine ?? 'claude';
  const label = ENGINE_LABEL[engine];
  const modes = permissionModes(engine);
  const problem = ccProblem(st, engine);
  useTitle(session?.title ?? '');

  // 세션을 열면 실시간 이벤트에 붙고 기록을 읽는다. 화면을 떠나면 보기만 멈춘다 (응답은 서버에서 계속)
  useEffect(() => {
    if (sessionId) void openSession(sessionId);
    else connectEvents(null);
    return () => disconnectEvents();
  }, [sessionId]);
  useEffect(() => { if (!sessionId && projectId) void loadExternal(projectId); }, [sessionId, projectId]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [chat]);
  useLayoutEffect(() => {
    const el = ta.current;
    if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 240)}px`; }
  }, [text]);
  const onScroll = () => {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 240;
  };

  const model = chosen?.model ?? '';
  const send = useCallback(async (msg = text) => {
    if (running || sending || (!msg.trim() && !files.length)) return;
    if (problem) return;
    stick.current = true;
    setSending(true);
    try {
      if (sessionId) {
        if (await sendTo(sessionId, msg, files)) { setText(''); setFiles([]); }
      } else {
        const id = await startAuto(msg, files, model, mode, base, ccProfile, engine === 'gemini' ? '' : ccEffort, pickedHarness, engine);
        if (id) { setText(''); setFiles([]); navigate(`/cc/${id}`); }
      }
    } finally {
      setSending(false);
    }
  }, [text, files, running, sending, problem, sessionId, base, model, mode, ccProfile, ccEffort, pickedHarness, engine, navigate]);

  // ───────── '/' 명령: 웹 명령(아래 선택 칸) + 이 CLI 가 알려 준 명령 (CLI 가 켜지면 그 대화 것, 아니면 그 엔진이 마지막으로 준 것)
  const [knownCmds, setKnownCmds] = useState<CliCommand[]>([]);
  useEffect(() => {
    let stale = false;
    ccApi.commands(engine, sessionId).then((r) => { if (!stale) setKnownCmds(r.commands); }).catch(() => undefined);
    return () => { stale = true; };
  }, [engine, sessionId]);
  const modelPick = useModelChoice({ running, value: session ? cliChoice(session.engine, session.model ?? '') : undefined,
    onCcModel: session ? (m) => patchSession(session.id, { model: m || null }) : undefined });
  const showInstructions = () => ui.sheet(`CLI 에 붙는 지침 — 용도 ${session ? session.profile || 'general' : ccProfile}`,
    session ? <InstructionsView sessionId={session.id} /> : <InstructionsView profile={ccProfile} harness={pickedHarness} />);
  const modeNow = session ? session.permission_mode : mode;
  const webCmds: SlashCommand[] = [
    { name: 'new', aliases: ['clear', 'reset', '새대화'], desc: `새 대화 (${label})`, kind: 'web', run: () => navigate('/') },
    { name: 'model', aliases: ['모델'], desc: '모델 바꾸기', kind: 'web', choices: () => modelChoices(modelPick), run: modelPick.choose },
    ...(engine !== 'gemini' ? [{ name: 'effort', aliases: ['추론'], desc: '추론 강도 (대화 중에 바꾸면 다음 메시지부터)', kind: 'web' as const,
      choices: () => pairChoices(EFFORT_LABELS, session ? session.effort ?? '' : ccEffort),
      run: (v: string) => (session ? patchSession(session.id, { effort: v as '' }) : setCcEffort(v)) }] : []),
    ...(!session && app.harnesses.some((h) => !h.auto) ? [{ name: 'harness', aliases: ['하네스'], desc: '하네스 고르기 (새 대화)', kind: 'web' as const,
      choices: () => harnessChoices(app.harnesses, pickedHarness), run: setHarness }] : []),
    ...(!session ? [{ name: 'role', aliases: ['용도', 'profile'], desc: '용도 바꾸기 (새 대화 — 대화 중에는 그대로)', kind: 'web' as const,
      choices: () => roleChoices(app.profiles, app.harnesses, pickedHarness, ccProfile), run: setCcProfile }] : []),
    { name: 'mode', aliases: ['승인', 'permissions'], desc: '승인 방식', kind: 'web',
      choices: () => modes.map(([v, l, d]) => ({ value: v, label: l.replace(/^[^:]+: /, ''), desc: d, current: v === modeNow })),
      run: (v) => (session ? patchSession(session.id, { permission_mode: v as 'default' | 'acceptEdits' | 'plan' }) : setMode(v)) },
    ...(session ? [{ name: 'rename', aliases: ['이름'], desc: '이 대화 이름 바꾸기', hint: '<새 이름>', kind: 'web' as const, needsArg: true,
      run: (v: string) => patchSession(session.id, { title: v }) }] : []),
    { name: 'instructions', aliases: ['지침'], desc: 'CLI 에 붙는 지침 보기 (설정 · 프롬프트 · 하네스)', kind: 'web', run: showInstructions },
    { name: 'attach', aliases: ['첨부', 'file'], desc: '파일 첨부', kind: 'web', run: () => fileInput.current?.click() },
  ];
  const slash = useSlash(text, setText, mergeCommands(webCmds,
    harnessCommands(app.commands, app.harnesses, session ? session.harness ?? '' : pickedHarness),
    engine === 'codex' ? templateCommands('AGENTS.md') : [],   // Codex 는 명령 목록을 주지 않는다 — 기본 요청 명령으로
    cliCommands(chat.commands ?? knownCmds, `CLI · ${label} — 보내면 CLI 가 실행`)), (t) => void send(t));

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (slash.onKeyDown(e)) return;
    if (e.key === 'Escape' && running && sessionId) { e.preventDefault(); void interrupt(sessionId); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
  };

  const external = useCc((s) => (projectId ? s.external[projectId] : undefined));
  const orphan = session?.process?.status === 'ORPHANED' ? session.process : null;
  const procStatus = session?.process?.status ?? (proc && !['EXITED', 'CRASHED', 'LOST'].includes(proc.status) ? proc.status : null);
  const result = chat.lastResult;
  // 사용량: 회사 Team 같은 구독 로그인이면 토큰만 ($ 는 CLI 가 API 정가로 환산한 추정이라 실제 청구가 아니다). API 키 로그인일 때만 $
  const plan = st?.auth?.subscriptionType;
  const spent = result ? usageText(result, engine === 'claude' && billedPerToken(chat.init?.api_key_source, st?.auth),
    engine === 'claude' ? `구독 계정${plan ? ` (${plan}${st?.auth?.orgName ? ` · ${st.auth.orgName}` : ''})` : ''}` : `${label} 계정 로그인`) : null;
  const cwd = session?.working_directory ?? base;

  return (
    <section className={`view active${!sessionId ? ' empty' : ''}`} id="view-chat">
      <div className="scroller" ref={scroller} onScroll={onScroll}
        onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); }}
        onDrop={(e) => { e.preventDefault(); setFiles((f) => [...f, ...Array.from(e.dataTransfer.files)]); }}>
        {sessionId && session && (
          <div className="cc-strip">
            <span className="cc-strip-path" title={session.working_directory}><Icon name="folder" size={14} /> {session.working_directory}</span>
            <span className={`chip${procStatus ? ' cc-on' : ''}`} title={proc?.cli_path ?? ''}>
              {procStatus ? `${PROCESS_LABEL[procStatus] ?? procStatus}${session.process?.pid ? ` · pid ${session.process.pid}` : ''}` : 'CLI 꺼짐 (보내면 자동 시작)'}
            </span>
            {chat.init?.model && <span className="muted small">{chat.init.model}</span>}
            <span className="spacer" />
            {procStatus && procStatus !== 'ORPHANED'
              ? <button type="button" className="mini-btn" title="CLI 프로세스를 닫습니다. 세션 기록은 남고, 다시 보내면 이어집니다." onClick={() => stopProcess(session.id)}><Icon name="stop" size={13} /><span>프로세스 닫기</span></button>
              : !procStatus && <button type="button" className="mini-btn" onClick={() => startProcess(session.id)}><Icon name="play" size={13} /><span>프로세스 시작</span></button>}
          </div>
        )}
        {sessionId ? <CcThread sessionId={sessionId} cwd={cwd} onRetry={(t) => send(t)} /> : (
          <div className="welcome">
            <h1 className="greeting">{greeting()}</h1>
            <div className="cc-welcome muted">
              <b>CLI · {label}</b> — 이 PC 에 설치된 {label} CLI 로 작업합니다.
              {' 보내면 '}{base ? <>기준 폴더 아래 <code className="inline">{base}</code></> : '기본 위치'}{' 에 새 작업 폴더를 만들어 그 안에서 작업합니다.'}
            </div>
            {engine === 'claude' && external && external.length > 0 && (
              <div className="cc-ext">
                <div className="muted small">이 폴더의 Claude Code 기록 (터미널·VS Code) — 눌러서 이어서 하기</div>
                {external.slice(0, 6).map((x) => (
                  <button key={x.claude_session_id} type="button" className="cc-ext-item" title={x.first_prompt ?? ''}
                    onClick={async () => { const id = await importExternal(projectId, x.claude_session_id); if (id) navigate(`/cc/${id}`); }}>
                    <Icon name="download" size={14} /><span className="t">{x.title}</span><span className="muted small">{timeAgo(x.last_modified)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="composer-wrap">
        {problem && problem !== '확인 중' && (
          <div className="model-banner down"><span className="msg">CLI · {label} 를 지금 쓸 수 없습니다 ({problem})
            <span className="sub">{st?.message ?? (problem === '로그인 필요' && engine === 'claude' ? '이 PC 에서 터미널에 claude 를 한 번 실행해 로그인하세요.'
              : problem === '로그인 필요' ? `관리 › 외부 AI 모델 › CLI 에서 ${label} '로그인'을 누르세요 (이 PC 의 브라우저에서 한 번).`
                : problem === '설치 필요' && engine !== 'claude' ? `관리 › 외부 AI 모델 › CLI 에서 ${label} '설치'를 누르세요 (이 PC 에 한 번).`
                  : '관리 › 외부 AI 모델 › CLI 에서 상태를 확인하세요.')}</span></span>
            <button type="button" className="btn" onClick={() => navigate('/admin/providers?view=cli')}>CLI 연결 보기</button>
            <button type="button" className="btn primary" onClick={() => app.setModelChoice('')}>로컬 모델로 바꾸기</button>
          </div>
        )}
        {orphan && (
          <div className="model-banner down"><span className="msg">이전 관리 서버가 띄운 CLI 프로세스(pid {orphan.pid})가 아직 이 세션을 쓰고 있습니다
            <span className="sub">같은 세션을 동시에 쓰지 않도록 끝날 때까지 새 메시지를 막습니다.</span></span>
            {orphan.registry_id != null && <button type="button" className="btn" onClick={() => terminateOrphan(orphan.registry_id!)}>이전 프로세스 종료</button>}
          </div>
        )}
        {session && !running && session.status === 'DISCONNECTED' && (
          <div className="model-banner down"><span className="msg">응답 도중 연결이 끊겼습니다<span className="sub">{session.last_error}</span></span>
            <button type="button" className="btn primary" onClick={() => send('중단된 작업을 이어서 진행해 주세요.')}>이어서 진행</button>
          </div>
        )}
        {session && !running && session.status === 'ERROR' && (chat.turnError || session.last_error) && (
          <div className="notice error cc-error"><span className="msg">{chat.turnError || session.last_error}</span>
            {chat.lastPromptText && <button type="button" className="btn" onClick={() => send(chat.lastPromptText)}>다시 시도</button>}
          </div>
        )}
        {sessionId && <PermissionPanel pending={chat.pending} sessionId={sessionId} cwd={cwd} />}
        <div className="composer">
          {files.length > 0 && (
            <div className="chips-row">
              {files.map((f, i) => (
                <span key={i} className="chip" title={f.name}>{f.type.startsWith('image/') ? '🖼' : '📄'} {f.name || '붙여넣은 이미지'} · {bytes(f.size)}
                  <button type="button" className="chip-x" aria-label="빼기" onClick={() => setFiles((x) => x.filter((_, j) => j !== i))}>×</button></span>
              ))}
            </div>
          )}
          {slash.menu}
          <textarea ref={ta} rows={1} value={text} autoFocus placeholder={running ? '응답 생성 중… (Esc 로 중단)' : `${label} 에게 시켜보세요 ( / 를 치면 명령)`}
            onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
            onPaste={(e) => {
              const imgs = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith('image/'));
              if (imgs.length) { e.preventDefault(); setFiles((f) => [...f, ...imgs]); }
            }} />
          <div className="composer-bar">
            <ModelSelect running={running} value={session ? cliChoice(session.engine, session.model ?? '') : undefined}
              onCcModel={session ? (m) => patchSession(session.id, { model: m || null }) : undefined} />
            {engine !== 'gemini' && (   // Gemini CLI 는 추론 강도를 따로 받지 않는다
              <select className="ghost-select" value={session ? (session.effort ?? '') : ccEffort}
                title="추론 강도 — 높을수록 더 오래 생각해 꼼꼼하지만 느리고 사용량이 늘어납니다. 대화 중에 바꾸면 다음 메시지부터"
                onChange={(e) => (session ? patchSession(session.id, { effort: e.target.value as '' }) : setCcEffort(e.target.value))}>
                {EFFORT_LABELS.map(([v, l]) => <option key={v || 'default'} value={v}>{l}</option>)}
              </select>
            )}
            {session ? (<>
              {session.harness && <span className="chip cc-profile-chip" title="이 대화의 하네스 (공통 하네스에 더해 붙음 — 대화 동안 그대로)">
                하네스: {sessionHarness?.title ?? session.harness}</span>}
              <span className="chip cc-profile-chip" title="이 대화의 용도 (첫 요청 때 지침이 붙어 대화 동안 그대로)">용도: {session.profile || 'general'}</span>
            </>) : (<>
              <HarnessSelect value={pickedHarness} onChange={setHarness} />
              <select className="ghost-select" value={ccProfile} title={app.profiles.find((p) => p.name === ccProfile)?.description ?? '용도'}
                onChange={(e) => setCcProfile(e.target.value)}>
                {harnessRoles.map((h) => (
                  <optgroup key={h.id} label={`용도 · ${h.title}`}>
                    {h.roles.map((p) => <option key={`${h.id}/${p.name}`} value={p.name} title={p.description}>{p.name}</option>)}
                  </optgroup>
                ))}
                <optgroup label={harnessRoles.length ? '용도 · 공통' : '용도 (설정 · 프롬프트 · 하네스 지침)'}>
                  {sharedRoles.map((p) => <option key={p.name} value={p.name} title={p.description}>{p.name}</option>)}</optgroup>
              </select>
            </>)}
            <button type="button" className="mini-btn" title="CLI 에 붙는 지침 보기 (로컬·API 와 같은 설정 · 프롬프트 · 하네스)"
              onClick={showInstructions}><span>지침</span></button>
            {session ? (
              <span className="chip mono cc-folder-chip" title={session.working_directory}><Icon name="folder" size={13} /> {session.working_directory.split(/[\\/]/).filter(Boolean).pop()}</span>
            ) : (
              <BaseDirSelect value={base} onChange={setBase} />
            )}
            <select className="ghost-select" value={session ? session.permission_mode : mode}
              title={modes.find(([v]) => v === (session ? session.permission_mode : mode))?.[2]}
              onChange={(e) => (session ? patchSession(session.id, { permission_mode: e.target.value as 'default' | 'acceptEdits' | 'plan' }) : setMode(e.target.value))}>
              {modes.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => { setFiles((f) => [...f, ...Array.from(e.target.files ?? [])]); e.target.value = ''; }} />
            <button type="button" className="icon-btn" title="파일 첨부 (끌어다 놓기 · 이미지 붙여넣기도 됨)" onClick={() => fileInput.current?.click()}><Icon name="paperclip" size={16} /></button>
            <span className="spacer" />
            <span className="muted small cc-result">
              {running ? (chat.turn?.statusText === 'retrying' ? <span className="warn-text">API 재시도 {chat.turn.retry?.attempt}/{chat.turn.retry?.max_retries}</span> : chat.pending.length ? '승인 대기' : '')
                : result ? <span title={spent?.title}>{`${result.interrupted ? '중단됨' : result.is_error ? '오류' : '완료'} · ${duration(result.duration_ms)}${spent?.text ? ` · ${spent.text}` : ''}`}</span> : ''}
            </span>
            <button type="button" className={`send-btn${running ? ' running' : ''}`} disabled={!running && (Boolean(problem) || sending)}
              title={running ? '중단 (Esc)' : '보내기 (Enter)'} aria-label={running ? '중단' : '보내기'}
              onClick={running ? () => sessionId && interrupt(sessionId) : () => send()}>
              {sending ? <Spinner /> : running ? <svg viewBox="0 0 20 20" width="14" height="14"><rect x="4" y="4" width="12" height="12" rx="2" fill="currentColor" /></svg>
                : <Icon name="send" size={18} />}
            </button>
          </div>
        </div>
        <div className="composer-note">CLI · {label} — 이 PC 의 {label} 계정과 파일 권한으로 일합니다. 승인 방식에 따라 수정·실행 전에 묻습니다.</div>
      </div>
    </section>
  );
}
