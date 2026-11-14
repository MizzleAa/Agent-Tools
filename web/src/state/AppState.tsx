// 여러 화면이 같이 보는 서버 상태: 시스템 상태(주기적으로 다시 읽음), 용도·워크플로 목록, 대화 목록,
// 입력창에서 고른 모델 ('' = 이 서버의 로컬 모델, 'ext:<연결>:<모델>' = 외부 AI 모델, 'cc:<모델>' = CLI · Claude Code (관리자) — 이 브라우저에 기억)
import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { api, type Schemas } from '@/api';
import { refreshCc } from '@/features/claudecode/actions';
import { ccProblem, parseCli } from '@/features/claudecode/lib';
import { useCc } from '@/features/claudecode/store';
import { useStored } from '@/lib/storage';
import { groupThreads, type Thread } from '@/lib/threads';
import { useAuth } from './Auth';

export type SystemStatus = Schemas['SystemStatus'];
export type ExternalModel = Schemas['ExternalModelInfo'];

interface AppState {
  status: SystemStatus | null;
  unreachable: boolean;               // 관리 서버 응답 없음
  modelReady: boolean;               // 고른 모델로 지금 보낼 수 있는지
  modelChoice: string;               // '' = 로컬 모델 · 'ext:…' = 외부 모델 · 'cc:…' = CLI · Claude Code
  external: ExternalModel | null;    // 고른 외부 모델 (로컬이면 null)
  setModelChoice: (v: string) => void;
  refreshStatus: () => void;
  profiles: Schemas['ProfileSummary'][];
  harnesses: Schemas['HarnessChoice'][];   // 회사·프로젝트 하네스 (고르면 그 하네스의 용도가 더해진다)
  workflows: Schemas['WorkflowSummary'][];
  commands: Schemas['HarnessCommand'][];   // 하네스 명령 (공통 + 쓸 수 있는 하네스 전부 — 입력창이 붙는 하네스 것만 쓴다)
  reloadCatalog: () => Promise<void>;
  threads: Thread[];
  reloadThreads: () => Promise<void>;
  schedules: Schemas['ScheduleOut'][];   // 예약 실행 (사이드바 '예약')
  reloadSchedules: () => Promise<void>;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error('AppStateProvider 밖');
  return v;
}

function isBusy(s: SystemStatus): boolean {
  return s.runtime.state !== 'ok' || s.intranet.sources.some((x) => x.crawl?.state === 'running')
    || s.models.some((m) => m.state === 'downloading');
}

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [profiles, setProfiles] = useState<Schemas['ProfileSummary'][]>([]);
  const [workflows, setWorkflows] = useState<Schemas['WorkflowSummary'][]>([]);
  const [harnesses, setHarnesses] = useState<Schemas['HarnessChoice'][]>([]);
  const [commands, setCommands] = useState<Schemas['HarnessCommand'][]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [schedules, setSchedules] = useState<Schemas['ScheduleOut'][]>([]);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [choice, setModelChoice] = useStored('model-choice', '');
  const { isAdmin } = useAuth();
  const adminRef = useRef(isAdmin);
  adminRef.current = isAdmin;
  const cc = useCc((s) => s.status);

  const poll = useCallback(async () => {
    clearTimeout(timer.current);
    let busy = true;
    try {
      const s = await api.system.status();
      setStatus(s);
      setUnreachable(false);
      busy = isBusy(s);
    } catch {
      setUnreachable(true);
    }
    if (adminRef.current) void refreshCc();   // CLI · Claude Code 상태·세션 목록 (관리자만 쓰는 기능)
    timer.current = setTimeout(poll, busy ? 2000 : 10000);   // 모델 적재·수집 중·연결 끊김이면 자주 확인
  }, []);

  const reloadCatalog = useCallback(async () => {
    const [p, w, h, c] = await Promise.all([api.catalog.profiles(), api.catalog.workflows().catch(() => []),
      api.catalog.harnesses().catch(() => []), api.catalog.commands().catch(() => [])]);
    setProfiles(p);
    setWorkflows(w);
    setHarnesses(h);
    setCommands(c);
  }, []);

  const reloadThreads = useCallback(async () => {
    try { setThreads(groupThreads(await api.runs.list(200))); } catch { /* 연결 끊김: 상태 배너가 알려 준다 */ }
  }, []);

  const reloadSchedules = useCallback(async () => {
    try { setSchedules(await api.schedules.list()); } catch { /* 연결 끊김: 상태 배너가 알려 준다 */ }
  }, []);

  useEffect(() => { if (isAdmin) void refreshCc(); }, [isAdmin]);

  // 예약 실행은 서버에서 시작되므로 30초마다 다시 읽는다 — 새로 시작한 실행이 있으면 최근 대화도
  const lastRuns = useRef('');
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const list = await api.schedules.list();
        setSchedules(list);
        const sig = list.map((s) => s.last_run_id).join(',');
        if (lastRuns.current && sig !== lastRuns.current) void reloadThreads();
        lastRuns.current = sig;
      } catch { /* 다음에 */ }
    }, 30000);
    return () => clearInterval(t);
  }, [reloadThreads]);

  useEffect(() => {
    poll();
    reloadCatalog().catch(() => {});
    reloadThreads();
    reloadSchedules();
    return () => clearTimeout(timer.current);
  }, [poll, reloadCatalog, reloadThreads, reloadSchedules]);

  // 고른 외부 모델이 없어졌으면(연결 삭제·꺼짐·폐쇄망 스위치) 로컬 모델로 돌아간다. CLI 는 관리자만 (쓸 수 없는 까닭은 화면이 알려 준다)
  const external = (choice && status?.external_models.find((m) => m.key === choice)) || null;
  const cli = parseCli(choice);
  const ccChosen = Boolean(cli) && isAdmin;
  const modelChoice = status && choice && !external && !ccChosen ? '' : choice;
  const ccReady = cli ? !ccProblem(cc, cli.engine) : false;   // 고른 CLI 엔진(Claude Code · Codex · Gemini)이 설치·로그인됐는지
  const value: AppState = {
    status, unreachable,
    modelReady: !unreachable && (ccChosen ? ccReady : external ? external.ready : status?.runtime.state === 'ok'),
    modelChoice, external, setModelChoice,
    refreshStatus: poll, profiles, harnesses, workflows, commands, reloadCatalog, threads, reloadThreads, schedules, reloadSchedules,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
