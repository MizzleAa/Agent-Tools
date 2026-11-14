// 입력창 '/' 명령 — 맨 앞에 / 를 치면 목록을 보여 준다 (Claude Code 처럼). 순수 함수만 (화면은 SlashMenu.tsx).
// web    화면 명령: 입력창의 선택 칸(모델 · 추론 · 하네스 · 용도 · 승인 …)을 바꾸거나 화면 일(사용량 보기 · 중지 …) — 보내지 않는다.
// prompt 요청 명령: 본문을 펼쳐(인자는 $ARGUMENTS 자리에) 요청으로 보낸다 — 하네스 commands/*.md 와 기본 /init · /review.
//        모델(로컬 · 외부 · CLI)과 상관없이 같게 쓴다.
// cli    CLI 명령: CLI 가 알려 준 명령 — 그대로 CLI 로 보낸다 (Claude Code · Gemini).
// 이름이 겹치면 화면 명령 → 하네스 명령 → 기본 요청 명령 → CLI 명령 순서로 앞의 것이 쓰인다 (mergeCommands).

export interface SlashChoice {
  value: string;
  label: string;
  desc?: string;
  current?: boolean;
  disabled?: boolean;
}

export interface SlashCommand {
  name: string;               // '/' 뒤 이름
  desc: string;
  kind: 'web' | 'prompt' | 'cli';
  group?: string;             // 목록의 묶음 이름 (비우면 kind 로)
  hint?: string;              // 인자 힌트 (보여 주기만)
  aliases?: string[];         // 다른 이름 (한국어 포함) — 쳐도 찾아진다
  title?: string;             // 마우스를 올리면 (CLI 원문 설명 등)
  choices?: () => SlashChoice[];   // 웹: 다음 단계에서 고를 값
  run?: (arg: string) => void;     // 웹: 고른 값(또는 친 인자)으로 실행
  needsArg?: boolean;              // 웹: 인자를 쳐야 실행 (예: /rename 새 이름)
  body?: string;                   // 요청: 보낼 글 ($ARGUMENTS = 뒤에 친 글)
}

export const GROUP_LABEL: Record<SlashCommand['kind'], string> = { web: '이 화면', prompt: '요청 — 보내면 모델이 실행', cli: 'CLI — 보내면 CLI 가 실행' };

/** 요청 명령의 본문을 펼친다: $ARGUMENTS 자리에 인자, 없으면 끝에 붙인다 */
export function expandPrompt(body: string, arg: string): string {
  const a = arg.trim();
  if (body.includes('$ARGUMENTS')) return body.split('$ARGUMENTS').join(a).trim();
  return a ? `${body}\n\n${a}` : body;
}

/** 인자를 받는 요청 명령인지 (고르면 바로 보내지 않고 '/이름 ' 까지 채워 인자를 기다린다) */
export const takesArg = (c: SlashCommand) => Boolean(c.hint) || Boolean(c.body?.includes('$ARGUMENTS'));

export type SlashView =
  | { stage: 'command'; items: SlashCommand[] }
  | { stage: 'choice'; command: SlashCommand; items: SlashChoice[] }
  | { stage: 'hint'; command: SlashCommand };   // 인자를 치는 중 — 도움말 한 줄만

/** '/이름' 또는 '/이름 인자' — 여러 줄이거나 '/' 로 시작하지 않으면 null */
export function parseSlash(text: string): { name: string; arg: string | null } | null {
  if (!text.startsWith('/') || text.includes('\n')) return null;
  const sp = text.search(/\s/);
  if (sp < 0) return { name: text.slice(1), arg: null };
  const name = text.slice(1, sp);
  return name ? { name, arg: text.slice(sp + 1) } : null;
}

const low = (s: string) => s.toLowerCase();
const namesOf = (c: SlashCommand) => [c.name, ...(c.aliases ?? [])].map(low);

export function findCommand(commands: SlashCommand[], name: string): SlashCommand | undefined {
  const n = low(name);
  return commands.find((c) => namesOf(c).includes(n));
}

/** 이름 · 다른 이름 · 설명으로 거른다. 정확히 같은 이름 → 앞부분이 같은 것 → 들어 있는 것 → 설명에 있는 것 순 */
export function matchCommands(commands: SlashCommand[], query: string): SlashCommand[] {
  const q = low(query.trim());
  if (!q) return commands;
  const score = (c: SlashCommand) => {
    const [name, ...aliases] = namesOf(c);
    if (name === q) return 0;
    if (aliases.includes(q)) return 1;
    if (name.startsWith(q)) return 2;
    if (aliases.some((a) => a.startsWith(q))) return 3;
    if (name.includes(q) || aliases.some((a) => a.includes(q))) return 4;
    return low(c.desc).includes(q) ? 5 : -1;
  };
  return commands.map((c) => [score(c), c] as const).filter(([s]) => s >= 0).sort((a, b) => a[0] - b[0]).map(([, c]) => c);
}

export function matchChoices(choices: SlashChoice[], query: string): SlashChoice[] {
  const q = low(query.trim());
  if (!q) return choices;
  const hit = (c: SlashChoice) => [c.value, c.label, c.desc ?? ''].some((s) => low(s).includes(q));
  const head = (c: SlashChoice) => low(c.label).startsWith(q) || low(c.value).startsWith(q);
  // 같으면 지금 값이 먼저 (Enter 로 바뀌지 않게), 고를 수 없는 값(연결 확인 중 · 로그인 필요 …)은 맨 뒤 — Enter 가 헛돌지 않게
  const rank = (c: SlashChoice) => (c.disabled ? -4 : 0) + (head(c) ? 2 : 0) + (c.current ? 1 : 0);
  return choices.filter(hit).sort((a, b) => rank(b) - rank(a));
}

/** 지금 친 글에 맞는 목록 — 보여 줄 것이 없으면 null (그냥 보내기) */
export function slashView(text: string, commands: SlashCommand[]): SlashView | null {
  const p = parseSlash(text);
  if (!p) return null;
  if (p.arg === null) {
    const items = matchCommands(commands, p.name);
    return items.length ? { stage: 'command', items } : null;
  }
  const command = findCommand(commands, p.name);
  if (!command) return null;
  if (command.choices) return { stage: 'choice', command, items: matchChoices(command.choices(), p.arg) };
  return { stage: 'hint', command };
}

/** 여러 목록을 합친다 — 앞 목록이 먼저: 뒤 목록의 이름이 앞과 겹치면 빼고, 겹치는 다른 이름만 뺀다
 *  (예: CLI 의 /model · /clear 는 화면의 선택 칸이 맡는다) */
export function mergeCommands(...lists: SlashCommand[][]): SlashCommand[] {
  const out: SlashCommand[] = [];
  const taken = new Set<string>();
  for (const list of lists) {
    const add = list.filter((c) => !taken.has(low(c.name))).map((c) => ({ ...c, aliases: (c.aliases ?? []).filter((a) => !taken.has(low(a))) }));
    add.forEach((c) => namesOf(c).forEach((n) => taken.add(n)));
    out.push(...add);
  }
  return out;
}

/** CLI 가 준 명령 (서버 /claude-code/commands 모양) → 목록 항목 */
export function cliCommands(list: { name: string; description?: string; description_en?: string; hint?: string; aliases?: string[] }[], group?: string): SlashCommand[] {
  return list.map((c) => ({
    name: c.name, desc: c.description ?? '', kind: 'cli', group, hint: c.hint || undefined, aliases: c.aliases,
    title: c.description_en && c.description_en !== c.description ? c.description_en : undefined,
  }));
}

/** 하네스 명령 (GET /commands) 중 이 입력창에 붙는 하네스 것만: 공통 + 늘 붙는 회사 하네스 + 고른 하네스.
 *  같은 이름이면 고른 하네스 → 회사 하네스 → 공통 순서로 (하네스 용도 찾는 순서와 같다) */
export function harnessCommands(list: { name: string; description: string; hint?: string; body: string; scope: string }[],
  harnesses: { id: string; title: string; auto: boolean }[], chosen: string): SlashCommand[] {
  const order = ['', ...harnesses.filter((h) => h.auto && h.id !== chosen).map((h) => h.id), ...(chosen ? [chosen] : [])];
  const title = (scope: string) => (scope ? harnesses.find((h) => h.id === scope)?.title ?? scope : '공통');
  const byName = new Map<string, SlashCommand>();
  for (const scope of order) {
    for (const c of list.filter((x) => x.scope === scope)) {
      byName.set(low(c.name), { name: c.name, desc: c.description, kind: 'prompt', group: `하네스 명령 · ${title(scope)} — 보내면 모델이 실행`,
        hint: c.hint || undefined, body: c.body, title: c.body.slice(0, 400) });
    }
  }
  return [...byName.values()];
}

/** 기본 요청 명령 (하네스에 같은 이름이 없을 때). file: 그 모델이 읽는 폴더 지침 파일 이름 */
export function templateCommands(file: string): SlashCommand[] {
  const group = '기본 요청 — 보내면 모델이 실행';
  return [
    { name: 'init', desc: `이 작업 폴더를 살펴 ${file}(폴더 지침)를 만든다`, kind: 'prompt', group, body:
      `이 작업 폴더의 파일을 살펴보고, 다음 작업부터 지침으로 쓸 ${file} 파일을 이 폴더 맨 위에 만들어 주세요.\n`
      + '- 무엇을 하는 프로젝트인지 (한두 줄)\n- 폴더 구조와 주요 파일\n- 빌드 · 실행 · 테스트 방법 (실제로 확인한 명령)\n'
      + '- 코드 작성 규칙 · 주의할 점\n이미 있으면 내용을 살려 고치고, 확인하지 못한 것은 추측으로 쓰지 마세요.' },
    { name: 'review', aliases: ['검토'], desc: '이 작업 폴더에서 만든 · 바꾼 것을 검토해 문제를 목록으로 (고치지 않음)', hint: '[살펴볼 파일 · 부분]', kind: 'prompt', group, body:
      '이 작업 폴더에서 만든 · 바꾼 것을 검토해 주세요. $ARGUMENTS\n'
      + '버그 · 빠진 예외 처리 · 실행되지 않는 부분 · 요청과 다른 동작을 찾아 심각한 것부터 목록으로 알려 주세요 '
      + '(파일:줄 · 무엇이 문제인지 · 근거). 이번에는 고치지 말고 보고만 합니다.' },
  ];
}
