// 왼쪽 사이드바: 새 작업 · 작업 폴더 · 파일 탐색기 · (관리자) 상태 · 설정·프롬프트(하네스 포함) · 관리 · (사용자) 내 계정 · 최근 대화(삭제) · 모델 상태 · 로그인한 사람
// 최근 대화에는 CLI · Claude Code 세션(관리자)도 시간순으로 섞어 보여 준다 (주소 /cc/<세션>).
import { useMemo } from 'react';
import { NavLink, useMatch, useNavigate } from 'react-router-dom';
import { api } from '@/api';
import type { Session } from '@/features/claudecode/api';
import { removeSession } from '@/features/claudecode/actions';
import { ccProblem, ENGINE_LABEL, parseCli, STATUS_DOT, STATUS_LABEL } from '@/features/claudecode/lib';
import { useCc } from '@/features/claudecode/store';
import { firstLine, fmtTime } from '@/lib/format';
import { STATUS } from '@/lib/labels';
import { statusDot, type Thread } from '@/lib/threads';
import { useApp } from '@/state/AppState';
import { useAuth } from '@/state/Auth';
import { Icon } from '@/ui/Icon';
import { useUi } from '@/ui/UiProvider';

export function Sidebar({ onNavigate }: { onNavigate: () => void }) {
  const { threads, reloadThreads } = useApp();
  const { isAdmin, user, state } = useAuth();
  const ui = useUi();
  const navigate = useNavigate();
  const open = useMatch('/c/:rootId');
  const openCc = useMatch('/cc/:ccId');
  const ccSessions = useCc((s) => s.sessions);
  const recents = useMemo(() => {   // 에이전트 대화 + CLI 세션을 최근 순으로
    const at = (v?: string | null) => (v ? Date.parse(v.endsWith('Z') || v.includes('+') ? v : v.replace(' ', 'T')) || 0 : 0);
    const items: ({ kind: 'run'; t: Thread; at: number } | { kind: 'cc'; s: Session; at: number })[] = [
      ...threads.map((t) => ({ kind: 'run' as const, t, at: at(t.last.created_at) })),
      ...(isAdmin ? Object.values(ccSessions).map((s) => ({ kind: 'cc' as const, s, at: at(s.last_used_at ?? s.created_at) })) : []),
    ];
    return items.sort((a, b) => b.at - a.at);
  }, [threads, ccSessions, isAdmin]);
  const go = (to: string) => { navigate(to); onNavigate(); };

  const remove = async (t: Thread) => {
    const title = firstLine(t.root.task).slice(0, 40);
    const extra = t.chain.length > 1 ? ` (이어서 한 ${t.chain.length - 1}개 포함)` : '';
    if (!(await ui.confirm({
      title: '대화를 삭제할까요?', confirmText: '삭제', danger: true,
      message: `'${title}'${extra} 대화 기록이 영구히 지워집니다.\nworkspace 의 작업 폴더와 파일은 그대로 남습니다.`,
    }))) return;
    try { await api.threads.remove(t.root.id); } catch (e) { ui.error(e); return; }
    if (open?.params.rootId === t.root.id) navigate('/');   // 보고 있던 대화를 지웠으면 새 작업 화면으로
    reloadThreads();
  };
  const removeCc = async (s: Session) => {
    if (!(await ui.confirm({
      title: 'CLI 대화를 목록에서 지울까요?', confirmText: '지우기', danger: true,
      message: `'${s.title.slice(0, 40)}' 를 목록에서 지웁니다.
Claude Code 의 세션 기록과 작업 폴더 파일은 그대로 남습니다 (다시 가져올 수 있음).`,
    }))) return;
    if (await removeSession(s.id) && openCc?.params.ccId === s.id) navigate('/');
  };

  return (
    <>
      <div className="sb-head">
        <span className="brand">{state.app_name}</span>
        <button type="button" className="icon-btn sb-close" aria-label="사이드바 닫기" onClick={onNavigate}><Icon name="x" size={18} /></button>
      </div>
      <button type="button" className="new-chat" onClick={() => go('/')}>
        <span className="plus"><Icon name="plus" size={14} /></span>새 작업
      </button>
      <nav className="sb-nav">
        <NavLink to="/files" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
          <Icon name="folder" size={16} />
          작업 폴더 · 코드
        </NavLink>
        <NavLink to="/explorer" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
          <Icon name="image" size={16} />
          파일 탐색기
        </NavLink>
        {isAdmin && <>
        <NavLink to="/status" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
          <svg viewBox="0 0 20 20" width="16" height="16"><circle cx="10" cy="10" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M10 2.5v2.2M10 15.3v2.2M2.5 10h2.2M15.3 10h2.2M4.7 4.7l1.6 1.6M13.7 13.7l1.6 1.6M4.7 15.3l1.6-1.6M13.7 6.3l1.6-1.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          상태 · 연결
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
          <svg viewBox="0 0 20 20" width="16" height="16"><path d="M4 5.5h7M15 5.5h1M4 10h2M10 10h6M4 14.5h9M17 14.5h-1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /><circle cx="13" cy="5.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.5" /><circle cx="8" cy="10" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.5" /><circle cx="14.5" cy="14.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
          설정 · 프롬프트
        </NavLink>
        <NavLink to="/admin" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
          <svg viewBox="0 0 20 20" width="16" height="16"><circle cx="7.5" cy="7" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M2.8 16c.6-2.7 2.4-4.2 4.7-4.2s4.1 1.5 4.7 4.2M13 4.6a2.4 2.4 0 0 1 0 4.8M14.6 11.9c1.5.5 2.5 1.8 2.8 4.1" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          관리
        </NavLink>
        </>}
        {user && (
          <NavLink to="/account" className={({ isActive }) => `sb-link${isActive ? ' active' : ''}`} onClick={onNavigate}>
            <svg viewBox="0 0 20 20" width="16" height="16"><circle cx="10" cy="7" r="3" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M4 17c.8-3.2 3-5 6-5s5.2 1.8 6 5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
            내 계정
          </NavLink>
        )}
      </nav>
      <div className="sb-section">최근</div>
      <div className="recents">
        {recents.length === 0 && <div className="empty">아직 작업이 없습니다.</div>}
        {recents.map((r) => r.kind === 'cc' ? (
          <div key={`cc-${r.s.id}`} className={`recent${openCc?.params.ccId === r.s.id ? ' active' : ''}`}>
            <button type="button" className="recent-open" title={`${r.s.title}
${r.s.working_directory}
CLI · Claude Code · ${STATUS_LABEL[r.s.status]}`}
              onClick={() => go(`/cc/${r.s.id}`)}>
              <span className="chip mono cc-recent-tag">CLI</span>
              <span className="t">{r.s.title}</span>
              <span className={`dot ${STATUS_DOT[r.s.status]}`} />
            </button>
            <button type="button" className="recent-del" title="목록에서 지우기" aria-label="목록에서 지우기" onClick={() => removeCc(r.s)}><Icon name="trash" size={15} /></button>
          </div>
        ) : ((t) => (
          <div key={t.root.id} className={`recent${open?.params.rootId === t.root.id ? ' active' : ''}`}>
            <button type="button" className="recent-open" title={`${t.root.task}\n${fmtTime(t.last.created_at)} · ${STATUS[t.last.status] ?? t.last.status}`}
              onClick={() => go(`/c/${t.root.id}`)}>
              <span className="t">{firstLine(t.root.task)}</span>
              <span className={`dot ${statusDot(t.last.status)}`} />
            </button>
            <button type="button" className="recent-del" title="대화 삭제" aria-label="대화 삭제" onClick={() => remove(t)}><Icon name="trash" size={15} /></button>
          </div>
        ))(r.t))}
      </div>
      <div className="sb-foot"><StatusPill />{user ? <UserBox /> : <SoloNote onNavigate={onNavigate} />}</div>
    </>
  );
}

/** 로그인 없이 쓰는 중(혼자 쓰기)이면: 그렇다는 것과 로그인 사용을 켜는 곳 */
function SoloNote({ onNavigate }: { onNavigate: () => void }) {
  const navigate = useNavigate();
  return (
    <div className="solo-note">
      <span className="muted small" title="이 서버에 들어온 사람은 누구나 관리자로 모든 기능을 씁니다">혼자 쓰기 · 로그인 없음</span>
      <button type="button" className="btn small-btn" onClick={() => { navigate('/admin/access'); onNavigate(); }}>로그인 사용 켜기</button>
    </div>
  );
}

function UserBox() {
  const { user, logout } = useAuth();
  const ui = useUi();
  if (!user) return null;
  return (
    <div className="user-box">
      <div className="user-who" title={`${user.username}${user.company_name ? ` · ${user.company_name}` : ''}`}>
        <b>{user.display_name || user.username}</b>
        <span className="muted small">{user.company_name || (user.role === 'admin' ? '관리자' : '')}</span>
      </div>
      <button type="button" className="btn small-btn" onClick={async () => {
        if (await ui.confirm({ title: '로그아웃할까요?', confirmText: '로그아웃' })) logout();
      }}>로그아웃</button>
    </div>
  );
}

function StatusPill() {
  const { status, unreachable, external, modelChoice, modelReady } = useApp();
  const cc = useCc((s) => s.status);
  if (unreachable) return <div className="pill bad">관리 서버 응답 없음</div>;
  if (!status) return <div className="pill">확인 중…</div>;
  const cli = parseCli(modelChoice);
  if (cli) {
    const info = cli.engine === 'claude' ? { version: cc?.cli?.version, path: cc?.cli?.path }
      : cc?.engines?.find((e) => e.engine === cli.engine);
    const problem = ccProblem(cc, cli.engine);
    const text = `CLI · ${ENGINE_LABEL[cli.engine]}${info?.version ? ` ${info.version}` : ''}${problem && problem !== '확인 중' ? ` · ${problem}` : ''}`;
    return <div className={`pill ${modelReady ? 'ok' : 'bad'}`} title={info?.path ?? ''}>⌘ {text}</div>;
  }
  if (external) {
    const text = `외부 모델 · ${external.connection_title} · ${external.title}`;
    return <div className={`pill ${external.ready ? 'ok' : 'bad'}`} title={external.problem || text}>☁ {text}</div>;
  }
  const { state, stage } = status.runtime;
  const text = state === 'ok' ? `모델 준비됨 · ${status.backend.model}`
    : stage ? `준비 중 · ${stage}` : state === 'loading' ? `모델 적재 중 · ${status.backend.model}` : '모델 서버 꺼짐';
  return <div className={`pill ${state === 'ok' ? 'ok' : state === 'loading' ? '' : 'bad'}`} title={text}>{text}</div>;
}
