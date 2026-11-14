import { describe, expect, it } from 'vitest';
import { cliCommands, expandPrompt, findCommand, harnessCommands, matchChoices, matchCommands, mergeCommands, parseSlash, type SlashCommand, slashView,
  takesArg, templateCommands } from './slash';

const web = (name: string, extra: Partial<SlashCommand> = {}): SlashCommand => ({ name, desc: `${name} 설명`, kind: 'web', ...extra });
const role = web('role', {
  aliases: ['용도'], desc: '용도 바꾸기',
  choices: () => [{ value: 'general', label: 'general' }, { value: 'coder', label: 'coder', current: true }, { value: 'reviewer', label: 'code-reviewer' }],
});
const cli = cliCommands([
  { name: 'compact', description: '대화를 요약', hint: '<instructions>' },
  { name: 'clear', description: 'Start a new session', aliases: ['reset', 'new'] },
  { name: 'code-review', description: '검토', aliases: ['review'] },
  { name: 'context', description: '컨텍스트 사용량', description_en: 'Show current context usage' },
]);
const all = mergeCommands([web('new', { aliases: ['clear', '새대화'] }), role, web('model', { aliases: ['모델'] })], cli);

describe('slash commands', () => {
  it('맨 앞의 / 만 명령으로 본다 (여러 줄 · 빈 이름 + 인자는 아님)', () => {
    expect(parseSlash('/com')).toEqual({ name: 'com', arg: null });
    expect(parseSlash('/role co')).toEqual({ name: 'role', arg: 'co' });
    expect(parseSlash('/role ')).toEqual({ name: 'role', arg: '' });
    expect(parseSlash('hi /x')).toBeNull();
    expect(parseSlash('/a\nb')).toBeNull();
    expect(parseSlash('/ x')).toBeNull();
  });

  it('겹치는 이름은 웹 명령이 맡고, CLI 명령의 겹치는 다른 이름도 뺀다', () => {
    expect(all.map((c) => c.name)).toEqual(['new', 'role', 'model', 'compact', 'code-review', 'context']);
    expect(findCommand(all, 'CLEAR')?.name).toBe('new');
    expect(findCommand(all, '용도')?.name).toBe('role');
    expect(all.find((c) => c.name === 'context')?.title).toBe('Show current context usage');
  });

  it('정확히 같은 이름 → 앞부분 → 들어 있는 것 → 설명 순으로 거른다', () => {
    expect(matchCommands(all, 'co').map((c) => c.name)).toEqual(['compact', 'code-review', 'context']);
    expect(matchCommands(all, 'review').map((c) => c.name)).toEqual(['code-review']);
    expect(matchCommands(all, '요약').map((c) => c.name)).toEqual(['compact']);
    expect(matchCommands(all, '').length).toBe(all.length);
  });

  it('목록 단계: 명령 → 값 고르기 → 인자 도움말, 맞는 것이 없으면 null', () => {
    expect(slashView('/mo', all)).toMatchObject({ stage: 'command', items: [{ name: 'model' }] });
    expect(slashView('/zzz', all)).toBeNull();
    expect(slashView('/home/user/a.txt 고쳐줘', all)).toBeNull();   // 경로로 시작하는 보통 요청은 방해하지 않는다
    const v = slashView('/용도 cod', all);
    expect(v?.stage).toBe('choice');
    expect(v?.stage === 'choice' && v.items.map((c) => c.value)).toEqual(['coder', 'reviewer']);
    const models = matchChoices([{ value: 'local:qwen3-8b', label: 'qwen3-8b' }, { value: 'local:qwen3.5-9b', label: 'qwen3.5-9b', current: true }], 'qwen');
    expect(models[0].value).toBe('local:qwen3.5-9b');   // 여럿이 맞으면 지금 값이 먼저 — Enter 로 모델 서버를 다시 띄우지 않게
    expect(slashView('/compact 짧게', all)).toMatchObject({ stage: 'hint', command: { name: 'compact', hint: '<instructions>' } });
  });

  it('요청 명령: $ARGUMENTS 자리에 인자, 없으면 끝에 붙인다', () => {
    expect(expandPrompt('$ARGUMENTS 를 검토해 줘.', ' a.py ')).toBe('a.py 를 검토해 줘.');
    expect(expandPrompt('보고서를 써 줘.', '짧게')).toBe('보고서를 써 줘.\n\n짧게');
    expect(expandPrompt('보고서를 써 줘.', '')).toBe('보고서를 써 줘.');
    const [init, review] = templateCommands('AGENTS.md');
    expect(init.body).toContain('AGENTS.md');
    expect(takesArg(init)).toBe(false);   // 고르면 바로 보낸다
    expect(takesArg(review)).toBe(true);  // '/review ' 까지 채우고 인자를 기다린다
  });

  it('하네스 명령: 붙는 하네스 것만, 같은 이름은 고른 하네스 → 회사 → 공통, 화면 명령 다음 · CLI 명령보다 먼저', () => {
    const list = [
      { name: 'report', description: '공통 보고', body: 'A', scope: '' },
      { name: 'report', description: '회사 보고', body: 'B', scope: 'acme' },
      { name: 'deploy', description: 'X 배포', body: 'C', scope: 'proj-x' },
      { name: 'other', description: '안 붙는 하네스', body: 'D', scope: 'proj-y' },
      { name: 'compact', description: '하네스 요약', body: 'E', scope: '' },
    ];
    const hs = [{ id: 'acme', title: 'ACME', auto: true }, { id: 'proj-x', title: '프로젝트 X', auto: false }, { id: 'proj-y', title: 'Y', auto: false }];
    const got = harnessCommands(list, hs, 'proj-x');
    expect(got.map((c) => [c.name, c.body])).toEqual([['report', 'B'], ['compact', 'E'], ['deploy', 'C']]);
    expect(got[0].group).toContain('ACME');
    expect(harnessCommands(list, hs, '').map((c) => c.name)).toEqual(['report', 'compact']);
    const merged = mergeCommands([web('new')], got, templateCommands('AGENT.md'), cli);
    expect(merged.find((c) => c.name === 'compact')?.kind).toBe('prompt');   // 하네스가 CLI 의 같은 이름을 가린다
    expect(merged.map((c) => c.name)).toEqual(['new', 'report', 'compact', 'deploy', 'init', 'review', 'clear', 'code-review', 'context']);
    expect(merged.find((c) => c.name === 'code-review')?.aliases).toEqual([]);   // 'review' 는 기본 요청 명령이 가져간다
  });
});
