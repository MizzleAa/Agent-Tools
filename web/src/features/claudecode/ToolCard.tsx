// 도구 호출 카드 (대화 화면의 details.tool 모양 그대로) + 코드 색 구분 · 수정 비교
import { memo, useMemo, type ReactNode } from 'react';
import { tokenize } from '@/features/files/highlight';
import { CopyButton } from '@/ui/CopyButton';
import { Icon, Spinner } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import type { ChatItem } from './items';
import { diffLines, diffStats, diffView, langOf, TOOL_LABEL, toolSummary } from './lib';
import type { ToolResultBlock, ToolUseBlock } from './types';

type Input = Record<string, unknown>;
const s = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v, null, 2));

/** 색 구분한 코드 (features/files/highlight 의 가벼운 토크나이저 — 외부 라이브러리 없이) */
export const Code = memo(function Code({ code, lang, max }: { code: string; lang?: string; max?: number }) {
  const lines = useMemo(() => tokenize(code, lang ?? ''), [code, lang]);
  return (
    <div className="codeblock cc-code">
      <div className="codeblock-head"><span>{lang || '텍스트'}</span><CopyButton text={code} /></div>
      <pre style={max ? { maxHeight: max } : undefined}><code>
        {lines.map((toks, i) => (
          <span key={i}>{toks.map((t, j) => (t.k === 'txt' ? t.v : <span key={j} className={`t-${t.k}`}>{t.v}</span>))}{i < lines.length - 1 ? '\n' : ''}</span>
        ))}
      </code></pre>
    </div>
  );
});

export const DiffView = memo(function DiffView({ before, after }: { before: string; after: string }) {
  const view = useMemo(() => diffView(diffLines(before, after)), [before, after]);
  return (
    <div className="cc-diff">
      {view.map((l, i) => (l === null
        ? <div key={i} className="cc-diff-gap">⋯</div>
        : <div key={i} className={`cc-diff-line ${l.t === '+' ? 'add' : l.t === '-' ? 'del' : ''}`}><span className="sign">{l.t}</span><span>{l.s || ' '}</span></div>))}
    </div>
  );
});

function stripLineNumbers(text: string): string {
  const lines = text.split('\n');
  if (lines.length > 1 && lines.slice(0, 5).every((l) => /^\s*\d+[\t→]/.test(l) || l === '')) return lines.map((l) => l.replace(/^\s*\d+[\t→]/, '')).join('\n');
  return text;
}

const ICON: Record<string, Parameters<typeof Icon>[0]['name']> = {
  Bash: 'terminal', PowerShell: 'terminal', Read: 'file', Write: 'file', Edit: 'edit', MultiEdit: 'edit', NotebookEdit: 'edit',
  Glob: 'search', Grep: 'search', WebFetch: 'globe', WebSearch: 'globe', TodoWrite: 'list', Task: 'bot', Agent: 'bot', AskUserQuestion: 'help',
};

/** 도구 입력 (승인 창에서도 쓴다) */
export function ToolInput({ use }: { use: ToolUseBlock }) {
  const input = use.input as Input;
  switch (use.name) {
    case 'Bash': case 'PowerShell':
      return <>{input.description ? <div className="muted small">{s(input.description)}</div> : null}<Code code={s(input.command)} lang={use.name === 'Bash' ? 'shell' : 'powershell'} /></>;
    case 'Edit': return <DiffView before={s(input.old_string)} after={s(input.new_string)} />;
    case 'MultiEdit': return <>{(Array.isArray(input.edits) ? (input.edits as Input[]) : []).map((e, i) => <DiffView key={i} before={s(e.old_string)} after={s(e.new_string)} />)}</>;
    case 'Write': return <Code code={s(input.content)} lang={langOf(s(input.file_path))} max={420} />;
    case 'Patch':   // Codex 파일 변경: 파일마다 diff
      return <>{(Array.isArray(input.changes) ? (input.changes as Input[]) : []).map((c, i) => (
        <div key={i}><div className="muted small mono">{s(c.kind) === 'add' ? '＋ ' : s(c.kind) === 'delete' ? '－ ' : ''}{s(c.path)}{c.move_path ? ` → ${s(c.move_path)}` : ''}</div>
          <Code code={s(c.diff)} lang="diff" max={420} /></div>
      ))}</>;
    case 'TodoWrite':
      return (
        <ul className="cc-todos">
          {((input.todos as { content?: string; status?: string; activeForm?: string }[]) ?? []).map((t, i) => (
            <li key={i} className={t.status}>{t.status === 'completed' ? '✔' : t.status === 'in_progress' ? '◐' : '○'} {t.status === 'in_progress' && t.activeForm ? t.activeForm : t.content}</li>
          ))}
        </ul>
      );
    case 'ExitPlanMode': return <div className="cc-plan"><Markdown text={s(input.plan)} /></div>;
    case 'AskUserQuestion':
      return <div>{(Array.isArray(input.questions) ? (input.questions as Input[]) : []).map((q, i) => (
        <div key={i}><b>{s(q.question)}</b> <span className="muted small">{(Array.isArray(q.options) ? (q.options as Input[]) : []).map((o) => s(o.label)).join(' · ')}</span></div>))}</div>;
    case 'Task': case 'Agent':
      return input.prompt ? <details className="cc-sub-prompt"><summary className="muted small">지시 내용</summary><Markdown text={s(input.prompt)} /></details> : null;
    case 'Read': return null;
    default: return Object.keys(input).length ? <pre className="block">{JSON.stringify(input, null, 2)}</pre> : null;
  }
}

function ToolResult({ use, result }: { use: ToolUseBlock; result: ToolResultBlock }) {
  const input = use.input as Input;
  const text = result.content || (result.is_error ? '(오류)' : '(출력 없음)');
  const more = result.truncated && <div className="muted small">… 결과가 길어 앞부분만 보여 줍니다.</div>;
  const imgs = result.images?.map((im, i) => (im.data ? <img key={i} className="cc-result-img" src={`data:${im.media_type};base64,${im.data}`} alt="" /> : null));
  if (result.is_error) return <><div className="lbl">결과</div><pre className="block err-text">{text}</pre>{more}</>;
  switch (use.name) {
    case 'Read': return <><Code code={stripLineNumbers(text)} lang={langOf(s(input.file_path))} max={420} />{more}{imgs}</>;
    case 'Edit': case 'MultiEdit': case 'Write': case 'Patch': case 'TodoWrite': return result.is_error ? <div className="muted small">{result.content}</div> : null;
    case 'Task': case 'Agent': case 'ExitPlanMode': return result.content ? <><div className="lbl">결과</div><div className="interim"><Markdown text={result.content} /></div></> : null;
    case 'AskUserQuestion': return <div className="muted small">{result.content}</div>;
    default: return <><div className="lbl">결과</div><pre className="block">{text}</pre>{more}{imgs}</>;
  }
}

const OPEN_BY_DEFAULT = new Set(['Edit', 'MultiEdit', 'Write', 'TodoWrite', 'ExitPlanMode', 'AskUserQuestion']);

export const ToolCard = memo(function ToolCard({ item, cwd, running, waiting, renderChildren }: {
  item: Extract<ChatItem, { kind: 'tool' }>; cwd: string; running: boolean; waiting: boolean; renderChildren: (items: ChatItem[]) => ReactNode;
}) {
  const { use, result, streamingInput, children } = item;
  const input = use.input as Input;
  const open = OPEN_BY_DEFAULT.has(use.name) || Boolean(result?.is_error) || waiting || children.length > 0;
  const state = result ? (result.is_error ? 'fail' : 'ok') : waiting ? 'wait' : '';
  let meta = '';
  if (use.name === 'Edit') { const d = diffStats(diffLines(s(input.old_string), s(input.new_string))); meta = `+${d.add} −${d.del}`; }
  else if (use.name === 'Write') meta = `+${s(input.content).split('\n').length}줄`;
  return (
    <details className="tool cc-tool" open={open || undefined}>
      <summary>
        <span className={`tool-icon ${state}`}>
          {result ? <Icon name={result.is_error ? 'x' : 'check'} size={15} /> : waiting ? <Icon name="hand" size={15} />
            : running || streamingInput !== undefined ? <Spinner /> : <Icon name={ICON[use.name] ?? 'terminal'} size={15} />}
        </span>
        <span className="tool-label">{TOOL_LABEL[use.name] ?? use.name}</span>
        <span className="tool-sum" title={toolSummary(use.name, input)}>{toolSummary(use.name, input, cwd)}</span>
        <span className="tool-meta">{waiting ? '승인 대기' : meta}</span>
        <Icon name="chevron" size={14} className="chev" />
      </summary>
      <div className="tool-body">
        {streamingInput !== undefined ? <pre className="block">{streamingInput || '…'}</pre> : <ToolInput use={use} />}
        {children.length > 0 && <div className="cc-sub">{renderChildren(children)}</div>}
        {result && <ToolResult use={use} result={result} />}
      </div>
    </details>
  );
});
