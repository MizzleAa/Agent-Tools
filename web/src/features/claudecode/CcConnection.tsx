// 관리 › 외부 AI 모델 › CLI: 이 PC 의 Claude Code · Codex · Gemini CLI 연결 상태 · 설정 · 작업 폴더 · CLI 프로세스
// 연결은 따로 할 일이 없다 — 설치·로그인된 claude.exe 를 자동으로 찾는다. 여기서는 확인하고, 필요하면 경로·기본값을 바꾼다.
import { useCallback, useEffect, useState } from 'react';
import { Badge, Field, Section, Toggle } from '@/ui/form';
import { Spinner } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';
import { type CcSettings, ccApi, type ProcessRow } from './api';
import { addProject, errText, refreshCc, terminateOrphan } from './actions';
import { FolderPicker } from '@/features/chat/FolderPicker';
import { useApp } from '@/state/AppState';
import { EngineSection } from './EngineSection';
import { InstructionsView } from './InstructionsView';
import { CC_MODELS, PERMISSION_MODES, PROCESS_LABEL, timeAgo } from './lib';
import { setState, useCc } from './store';

export function CcConnection() {
  const ui = useUi();
  const st = useCc((s) => s.status);
  const settings = useCc((s) => s.settings);
  const projects = useCc((s) => s.projects);
  const [draft, setDraft] = useState<CcSettings | null>(settings);
  const [checking, setChecking] = useState(false);
  const [procs, setProcs] = useState<ProcessRow[]>([]);
  const [picking, setPicking] = useState(false);
  const { profiles } = useApp();
  const [insProfile, setInsProfile] = useState('general');
  useEffect(() => setDraft(settings), [settings]);

  const check = async () => { setChecking(true); await refreshCc(true); setChecking(false); };
  useEffect(() => { void check(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const loadProcs = useCallback(async () => { try { setProcs(await ccApi.processes()); } catch { /* 설치 전 */ } }, []);
  useEffect(() => {
    void loadProcs();
    const t = setInterval(loadProcs, 4000);
    return () => clearInterval(t);
  }, [loadProcs]);

  if (!st) return <div className="muted"><Spinner /> 확인하는 중…</div>;
  if (!st.installed) {
    return (
      <Section title="CLI · Claude Code" help="이 PC 에 설치·로그인된 Claude Code 를 대화 화면에서 '⌘ Claude Code' 모델로 씁니다.">
        <div className="notice error"><span className="msg">{st.message}</span></div>
      </Section>
    );
  }
  const cli = st.cli;
  const auth = st.auth;
  const ok = Boolean(cli?.found && auth?.loggedIn && !st.offline);
  const changed = settings && draft ? (Object.keys(draft) as (keyof CcSettings)[]).filter((k) => draft[k] !== settings[k]) : [];
  const save = async () => {
    if (!draft) return;
    try {
      const body: Record<string, unknown> = {};
      for (const k of changed) body[k] = draft[k];
      setState({ settings: await ccApi.saveSettings(body) });
      ui.toast('저장했습니다', 'ok');
      if ('cli_path' in body || 'codex_path' in body || 'gemini_path' in body) await check();
    } catch (e) { ui.toast(errText(e), 'error'); }
  };
  const set = <K extends keyof CcSettings>(k: K, v: CcSettings[K]) => draft && setDraft({ ...draft, [k]: v });
  const editProject = async (id: string) => {
    const p = projects.find((x) => x.id === id);
    if (!p) return;
    const v = await ui.form({ title: `'${p.name}' 고치기`, confirmText: '저장', fields: [
      { name: 'name', label: '이름', value: p.name },
      { name: 'path', label: '폴더', value: p.path, help: '바꾸면 새 대화부터 새 폴더에서 합니다. 기존 대화는 원래 폴더를 그대로 씁니다 (Claude Code 기록이 그 폴더 기준).' }] });
    if (!v) return;
    try {
      const r = await ccApi.updateProject(id, { name: v.name, path: v.path !== p.path ? v.path : null });
      ui.toast(r.sessions_on_old_path ? `저장했습니다. 기존 대화 ${r.sessions_on_old_path}개는 원래 폴더를 씁니다.` : '저장했습니다', 'ok', 6000);
      await refreshCc();
    } catch (e) { ui.toast(errText(e), 'error'); }
  };
  const removeProject = async (id: string) => {
    const p = projects.find((x) => x.id === id);
    if (!p || !(await ui.confirm({ title: `'${p.name}' 를 목록에서 지울까요?`, danger: true, confirmText: '지우기',
      message: '이 폴더의 CLI 대화도 목록에서 빠집니다. 폴더의 파일과 Claude Code 기록은 그대로 남습니다.' }))) return;
    try { await ccApi.deleteProject(id); await refreshCc(); } catch (e) { ui.toast(errText(e), 'error'); }
  };
  const live = procs.filter((r) => r.live);

  return (
    <>
      <Section title={<span className="title">CLI <span className="muted small">· Claude Code (이 서버 PC)</span></span>}
        help="설치·로그인된 Claude Code 를 자동으로 찾아 씁니다. 대화 화면의 모델 선택에서 '⌘ Claude Code' 를 고르면 같은 화면에서 작업합니다 (관리자만)."
        right={<>
          {st.offline ? <Badge kind="fail">폐쇄망 스위치로 꺼짐</Badge> : ok ? <Badge kind="ok">연결됨</Badge> : <Badge kind="fail">연결 안 됨</Badge>}
          <button type="button" className="btn" disabled={checking} onClick={check}>{checking ? '확인하는 중…' : '연결 확인'}</button>
        </>}>
        {st.offline && <div className="notice error"><span className="msg">폐쇄망 스위치가 켜져 있어 CLI 를 쓰지 않습니다 (접속 설정에서 끌 수 있음).</span></div>}
        <dl className="kv">
          <dt>CLI</dt><dd>{cli?.found ? <>{cli.version} <span className="muted">({cli.source})</span></> : <span className="err-text">{cli?.error ?? '찾지 못함'}</span>}</dd>
          <dt>경로</dt><dd className="small"><code className="inline">{cli?.path ?? '-'}</code></dd>
          <dt>로그인</dt><dd>{auth?.loggedIn ? <>{auth.email} <span className="muted small">· {auth.authMethod} · {auth.orgName} · {auth.subscriptionType}</span></>
            : <span className="err-text">{auth?.error ?? '로그인되어 있지 않습니다 — 이 PC 의 터미널에서 claude 를 한 번 실행해 로그인하세요.'}</span>}</dd>
          <dt>프로세스</dt><dd>실행 중 {st.alive} / 최대 {st.max_processes}{st.busy ? ` · 응답 중 ${st.busy}` : ''}{st.orphaned ? ` · 이전 서버가 남긴 ${st.orphaned}` : ''}</dd>
          <dt>기타</dt><dd className="small muted">Agent SDK {st.sdk_version} · 데이터 {st.home}</dd>
        </dl>
      </Section>

      {draft && (
        <Section title="설정" right={<button type="button" className="btn primary" disabled={!changed.length} onClick={save}>저장</button>}>
          <div className="field-grid">
            <Field label="CLI 경로 직접 지정" help={`비우면 자동 (${(cli?.candidates ?? []).map((c) => c.source).join(' · ') || '후보 없음'})`}>
              <input type="text" value={draft.cli_path} placeholder={cli?.path ?? ''} onChange={(e) => set('cli_path', e.target.value)} /></Field>
            <Field label="새 대화 기본 모델"><select className="select" value={draft.default_model} onChange={(e) => set('default_model', e.target.value)}>
              {CC_MODELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
            <Field label="새 대화 기본 승인 방식" help="승인 절차를 건너뛰는 모드는 없습니다"><select className="select" value={draft.default_permission_mode}
              onChange={(e) => set('default_permission_mode', e.target.value as CcSettings['default_permission_mode'])}>
              {PERMISSION_MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
            <Field label="쉬는 CLI 닫기 (분)" help="세션 기록은 남고, 다시 보내면 이어집니다">
              <input type="number" min={1} value={draft.idle_process_timeout_min} onChange={(e) => set('idle_process_timeout_min', Number(e.target.value))} /></Field>
            <Field label="동시 CLI 최대 수"><input type="number" min={1} max={32} value={draft.max_processes} onChange={(e) => set('max_processes', Number(e.target.value))} /></Field>
          </div>
          <Toggle checked={draft.prewarm_process} onChange={(v) => set('prewarm_process', v)} label="대화를 열 때 CLI 를 미리 시작" help="첫 응답이 빨라집니다" />
          <Toggle checked={draft.show_thinking} onChange={(v) => set('show_thinking', v)} label="생각 과정 보이기" />
          <Toggle checked={draft.show_external_sessions} onChange={(v) => set('show_external_sessions', v)} label="터미널·VS Code 에서 만든 Claude Code 기록도 보이기" help="새 대화 화면에서 눌러 이어서 할 수 있습니다" />
        </Section>
      )}

      {(st.engines ?? []).map((e) => <EngineSection key={e.engine} info={e} offline={st.offline} draft={draft} set={set} />)}

      <Section title="CLI 에 붙는 지침" help="로컬·API 모델과 같은 '설정 · 프롬프트'(용도·블록)와 하네스(지침·자료·용도)를 CLI 기본 지침 뒤에 덧붙입니다 (Codex 는 개발자 지침으로, Gemini 는 대화 첫 요청 앞에). 대화마다 입력창에서 하네스 · 용도를 고릅니다."
        right={<select className="select" value={insProfile} onChange={(e) => setInsProfile(e.target.value)}>{profiles.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select>}>
        <InstructionsView profile={insProfile} />
      </Section>

      <Section title="작업 폴더" help="CLI 대화는 폴더마다 나뉩니다. 대화 화면의 폴더 선택에서도 추가할 수 있습니다."
        right={<button type="button" className="btn" onClick={() => setPicking(true)}>폴더 추가</button>}>
        {projects.length === 0 ? <div className="muted small">아직 없습니다.</div> : (
          <table className="set-table">
            <thead><tr><th>이름</th><th>폴더</th><th style={{ width: 70 }}>대화</th><th style={{ width: 140 }} /></tr></thead>
            <tbody>{projects.map((p) => (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td className="small"><code className="inline">{p.path}</code>{!p.path_exists && <span className="err-text small"> (없음)</span>}</td>
                <td>{p.session_count}</td>
                <td><button type="button" className="btn small-btn" onClick={() => editProject(p.id)}>고치기</button>{' '}
                  <button type="button" className="btn small-btn danger" onClick={() => removeProject(p.id)}>지우기</button></td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Section>

      <Section title={`CLI 프로세스 (실행 중 ${live.length})`} help="세션과 프로세스는 별개입니다. 프로세스가 끝나도 대화 기록은 남고, 보내면 같은 세션으로 다시 시작합니다.">
        {procs.length === 0 ? <div className="muted small">기록이 없습니다.</div> : (
          <table className="set-table">
            <thead><tr><th>상태</th><th>PID</th><th>대화</th><th>시작</th><th /></tr></thead>
            <tbody>{procs.slice(0, 15).map((r) => (
              <tr key={r.registry_id}>
                <td><Badge kind={r.live ? 'ok' : ''}>{PROCESS_LABEL[r.status] ?? r.status}</Badge></td>
                <td><code className="inline">{r.pid}</code></td>
                <td className="small">{r.session_title ?? <span className="muted">(지운 대화)</span>}{r.project_name && <span className="muted"> · {r.project_name}</span>}</td>
                <td className="small">{timeAgo(r.started_at)}</td>
                <td>{r.status === 'ORPHANED' && r.live && <button type="button" className="btn small-btn danger" onClick={async () => { await terminateOrphan(r.registry_id); void loadProcs(); }}>종료</button>}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Section>
      {picking && <FolderPicker title="작업 폴더 추가" help="추가한 폴더는 목록에서 이름·경로를 관리합니다 (그 폴더의 터미널·VS Code 기록도 대화 화면에서 이어서 할 수 있음)." confirmText="추가"
        onClose={() => setPicking(false)} onPicked={async (_rel, full) => { await addProject(full); void refreshCc(); }} />}
    </>
  );
}
