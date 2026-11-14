// Claude Code(CLI) 화면 도우미: 상태 이름 · 도구 요약 · '항상 허용' 문장 · 줄 비교(diff) · 시간·비용 표시
import type { CcStatus, SessionStatus } from './api';
import type { PermissionSuggestion } from './types';

export const STATUS_LABEL: Record<SessionStatus, string> = {
  IDLE: '대기', RUNNING: '응답 중', WAITING_FOR_INPUT: '답변 필요', WAITING_FOR_PERMISSION: '승인 필요',
  COMPLETED: '완료', ERROR: '오류', DISCONNECTED: '연결 끊김',
};
/** 사이드바 점 색 (lib/threads.ts statusDot 과 같은 이름) */
export const STATUS_DOT: Record<SessionStatus, string> = {
  IDLE: '', RUNNING: 'running', WAITING_FOR_INPUT: 'stuck', WAITING_FOR_PERMISSION: 'stuck', COMPLETED: '', ERROR: 'error', DISCONNECTED: 'stuck',
};
export const PROCESS_LABEL: Record<string, string> = {
  STARTING: 'CLI 시작 중', READY: 'CLI 연결됨', BUSY: 'CLI 작업 중', STOPPING: 'CLI 종료 중', EXITED: 'CLI 종료됨',
  CRASHED: 'CLI 비정상 종료', ORPHANED: '이전 CLI 실행 중', LOST: 'CLI 사라짐',
};
export const PERMISSION_MODES: [string, string, string][] = [
  ['default', '승인: 매번 묻기', '파일 수정·명령 실행 전에 묻습니다'],
  ['acceptEdits', '승인: 편집 자동', '파일 편집은 묻지 않고, 명령 실행은 묻습니다'],
  ['plan', '승인: 계획 모드', '읽기만 하며 계획을 세우고, 실행 전에 승인받습니다'],
];
/** Codex · Gemini 의 권한 모드 — 같은 세 값을 이 서버가 승인 처리에서 맞춘다 (agent/claude_code/manager.py _engine_ask) */
const ENGINE_MODES: Record<'codex' | 'gemini', [string, string, string][]> = {
  codex: [
    ['default', '승인: 매번 묻기', '명령 실행·파일 수정 전에 묻습니다 (읽기만 하는 명령은 Codex 가 묻지 않고 실행)'],
    ['acceptEdits', '승인: 편집 자동', '파일 수정은 묻지 않고, 명령 실행은 묻습니다'],
    ['plan', '승인: 계획만', '명령 실행·파일 수정은 하지 않고, 읽어 보며 계획만 세웁니다'],
  ],
  gemini: [
    ['default', '승인: 매번 묻기', '파일 수정·명령 실행 전에 묻습니다'],
    ['acceptEdits', '승인: 편집 자동', '파일 수정은 묻지 않고, 명령 실행은 묻습니다'],
    ['plan', '승인: 계획 모드', 'Gemini 계획 모드 — 읽기만 하며 계획을 세웁니다'],
  ],
};
export const permissionModes = (engine: CcEngine) => (engine === 'claude' ? PERMISSION_MODES : ENGINE_MODES[engine]);

// CLI 엔진: 입력창 모델 선택값 '<접두>:<모델>' ('' = 그 CLI 의 기본 모델) — cc: Claude Code · cx: Codex · gm: Gemini
export type CcEngine = 'claude' | 'codex' | 'gemini';
export const ENGINES: CcEngine[] = ['claude', 'codex', 'gemini'];
export const ENGINE_LABEL: Record<CcEngine, string> = { claude: 'Claude Code', codex: 'Codex', gemini: 'Gemini' };
const PREFIX: Record<CcEngine, string> = { claude: 'cc', codex: 'cx', gemini: 'gm' };
export const CC_MODELS: [string, string][] = [['', '기본 모델'], ['opus', 'Opus'], ['sonnet', 'Sonnet'], ['haiku', 'Haiku']];
export function parseCli(choice: string): { engine: CcEngine; model: string } | null {
  const engine = ENGINES.find((e) => choice.startsWith(`${PREFIX[e]}:`));
  return engine ? { engine, model: choice.slice(PREFIX[engine].length + 1) } : null;
}
export const cliChoice = (engine: CcEngine, model = '') => `${PREFIX[engine]}:${model}`;
export const isCc = (choice: string) => parseCli(choice) !== null;

/** 그 CLI 를 지금 쓸 수 없는 까닭 ('' = 쓸 수 있음) */
export function ccProblem(st: CcStatus | null | undefined, engine: CcEngine = 'claude'): string {
  if (!st) return '확인 중';
  if (!st.installed) return '설치 필요';
  if (st.offline) return '폐쇄망 스위치로 꺼짐';
  if (engine === 'claude') return !st.cli?.found ? 'CLI 없음' : !st.auth?.loggedIn ? '로그인 필요' : '';
  const e = (st.engines ?? []).find((x) => x.engine === engine);
  if (!e) return '확인 중';
  if (!e.installed) return '설치 필요';
  return e.logged_in ? '' : '로그인 필요';
}

export function timeAgo(iso: string | number | null | undefined): string {
  if (!iso) return '';
  const t = typeof iso === 'number' ? iso : Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return '방금';
  if (s < 3600) return `${Math.round(s / 60)}분 전`;
  if (s < 86400) return `${Math.round(s / 3600)}시간 전`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)}일 전`;
  return new Date(t).toLocaleDateString('ko-KR');
}
export const bytes = (n?: number | null) => (n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
export const duration = (ms: number) => (ms < 1000 ? `${ms}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}초` : `${Math.floor(ms / 60000)}분 ${Math.round((ms % 60000) / 1000)}초`);
export const cost = (usd?: number | null) => (usd == null ? '' : usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`);

export function shortPath(p: string, base?: string | null): string {
  if (!p || !base) return p;
  const a = p.replace(/\//g, '\\').toLowerCase();
  const b = base.replace(/\//g, '\\').replace(/\\$/, '').toLowerCase();
  return a.startsWith(b + '\\') ? p.slice(b.length + 1) : p;
}
export const extOf = (p: string) => /\.([a-z0-9]+)$/i.exec(p)?.[1].toLowerCase() ?? '';
const LANG: Record<string, string> = { py: 'python', js: 'javascript', jsx: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript', cs: 'csharp',
  java: 'java', go: 'go', rs: 'rust', ps1: 'powershell', sh: 'shell', bash: 'shell', sql: 'sql', json: 'json', yml: 'yaml', yaml: 'yaml', toml: 'toml', ini: 'ini', css: 'css' };
export const langOf = (p: string) => LANG[extOf(p)] ?? '';

type Input = Record<string, unknown>;
const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v));

export const TOOL_LABEL: Record<string, string> = {
  Bash: '명령 실행', PowerShell: '명령 실행', Read: '파일 읽기', Write: '파일 쓰기', Edit: '파일 수정', MultiEdit: '파일 수정',
  NotebookEdit: '노트북 수정', Glob: '파일 찾기', Grep: '내용 검색', WebFetch: '웹 페이지', WebSearch: '웹 검색', TodoWrite: '할 일',
  Task: '서브에이전트', Agent: '서브에이전트', AskUserQuestion: '질문', ExitPlanMode: '계획 승인', Skill: '스킬',
  Patch: '파일 수정',
};

export function toolSummary(name: string, input: Input, cwd?: string | null): string {
  switch (name) {
    case 'Bash': case 'PowerShell': return str(input.command);
    case 'Read': case 'Write': case 'Edit': case 'MultiEdit': case 'NotebookEdit': return shortPath(str(input.file_path ?? input.notebook_path), cwd);
    case 'Glob': return str(input.pattern) + (input.path ? `  (${shortPath(str(input.path), cwd)})` : '');
    case 'Grep': return `"${str(input.pattern)}"` + (input.path ? `  ${shortPath(str(input.path), cwd)}` : '');
    case 'WebFetch': return str(input.url);
    case 'WebSearch': return str(input.query);
    case 'TodoWrite': return `${Array.isArray(input.todos) ? input.todos.length : 0}개 항목`;
    case 'Task': case 'Agent': return str(input.description) + (input.subagent_type ? ` · ${str(input.subagent_type)}` : '');
    case 'AskUserQuestion': { const q = Array.isArray(input.questions) ? (input.questions[0] as Input | undefined) : undefined; return q ? str(q.question) : ''; }
    case 'ExitPlanMode': return '계획을 실행해도 될까요?';
    case 'Patch': {   // Codex 파일 변경 (파일 여러 개일 수 있다)
      const ch = Array.isArray(input.changes) ? (input.changes as Input[]) : [];
      return ch.length ? shortPath(str(ch[0].path), cwd) + (ch.length > 1 ? ` 외 ${ch.length - 1}개` : '') : '';
    }
    default: { const first = Object.values(input).find((v) => typeof v === 'string'); return first ? str(first).slice(0, 200) : ''; }
  }
}

const DEST: Record<string, string> = {
  session: '이 세션 동안', localSettings: '이 프로젝트에서 항상 (로컬 설정)', projectSettings: '이 프로젝트에서 항상 (공유 설정)',
  userSettings: '모든 프로젝트에서 항상', cliArg: '이번 실행 동안',
};
const clip = (t: string, n = 60) => (t.length > n ? t.slice(0, n - 1) + '…' : t);
const sameDir = (a: string, b: string) => a.replace(/[\\/]+$/, '').toLowerCase() === b.replace(/[\\/]+$/, '').toLowerCase();

/** CLI 가 제안한 '항상 허용' 옵션을 문장으로 */
export function suggestionLabel(s: PermissionSuggestion, cwd?: string | null): string {
  const where = DEST[s.destination ?? ''] ?? s.destination ?? '';
  if (s.type === 'setMode') return `${s.mode === 'acceptEdits' ? '파일 편집 자동 허용' : s.mode ?? ''} 켜기 — ${where}`;
  if (s.type === 'addRules' || s.type === 'replaceRules') {
    const rules = clip((s.rules ?? []).map((r) => (r.ruleContent ? `${r.toolName}(${r.ruleContent})` : r.toolName)).join(', '));
    return `${s.behavior === 'deny' ? '항상 거부' : s.behavior === 'ask' ? '항상 묻기' : '허용'}: ${rules} — ${where}`;
  }
  if (s.type === 'addDirectories') {
    const dirs = (s.directories ?? []).map((d) => (cwd && sameDir(d, cwd) ? '프로젝트 폴더' : clip(shortPath(d, cwd), 50)));
    return `폴더 접근 허용: ${dirs.join(', ')} — ${where}`;
  }
  return `${s.type} — ${where}`;
}

// ───────── 줄 비교 (작은 LCS — 파일 수정 카드용, 아주 크면 통째로 바뀜으로)
export interface DiffLine { t: ' ' | '+' | '-'; s: string }

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.replace(/\n$/, '').split('\n');
  const b = after.replace(/\n$/, '').split('\n');
  if (a.length * b.length > 4_000_000) return [...a.map((s) => ({ t: '-' as const, s })), ...b.map((s) => ({ t: '+' as const, s }))];
  const n = a.length, m = b.length;
  const dp: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: ' ', s: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ t: '-', s: a[i++] });
    else out.push({ t: '+', s: b[j++] });
  }
  while (i < n) out.push({ t: '-', s: a[i++] });
  while (j < m) out.push({ t: '+', s: b[j++] });
  return out;
}

/** 바뀐 줄 앞뒤 문맥만 남긴다 (null = 생략 표시) */
export function diffView(lines: DiffLine[], context = 3): (DiffLine | null)[] {
  const keep = lines.map(() => false);
  lines.forEach((l, i) => { if (l.t !== ' ') for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k++) keep[k] = true; });
  const view: (DiffLine | null)[] = [];
  lines.forEach((l, i) => { if (keep[i]) view.push(l); else if (view[view.length - 1] !== null) view.push(null); });
  return view;
}

export const diffStats = (lines: DiffLine[]) => ({ add: lines.filter((l) => l.t === '+').length, del: lines.filter((l) => l.t === '-').length });
