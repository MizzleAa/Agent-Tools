// 정규화된 메시지 목록 → 화면 항목.
// - 도구 호출(tool_use)과 결과(tool_result)를 하나의 카드로 묶는다.
// - 서브에이전트 메시지(parent_tool_use_id)는 부모 도구 카드 안에 넣는다.
// - 스트리밍 중인 조각(partial)은 맨 뒤에 붙는다.
import type { NormMessage, PartialBlock, ToolResultBlock, ToolUseBlock } from './types';
import type { Notice } from './chat';

export type ChatItem =
  | { kind: 'prompt'; key: string; msg: NormMessage }
  | { kind: 'text'; key: string; text: string; streaming?: boolean; error?: string }
  | { kind: 'thinking'; key: string; text: string; streaming?: boolean }
  | { kind: 'tool'; key: string; use: ToolUseBlock; result?: ToolResultBlock; children: ChatItem[]; streamingInput?: string }
  | { kind: 'notice'; key: string; level: 'info' | 'warning' | 'error'; text: string }
  | { kind: 'meta'; key: string; text: string };

const META_TAG = /<\/?(command-name|command-message|command-args|local-command-stdout|local-command-stderr|bash-input|bash-stdout|bash-stderr)>/g;

function metaText(m: NormMessage): string {
  return m.blocks
    .filter((b) => b.type === 'text')
    .map((b) => (b as { text: string }).text.replace(META_TAG, ' ').replace(/\s+\n/g, '\n').trim())
    .join('\n')
    .trim();
}

export function buildItems(messages: NormMessage[], partial: PartialBlock[], notices: Notice[]): ChatItem[] {
  const results = new Map<string, ToolResultBlock>();
  for (const m of messages) {
    if (m.role !== 'user') continue;
    for (const b of m.blocks) if (b.type === 'tool_result') results.set(b.tool_use_id, b);
  }
  const tools = new Map<string, Extract<ChatItem, { kind: 'tool' }>>();
  const top: ChatItem[] = [];
  const pendingNotices = [...notices].sort((a, b) => a.afterCount - b.afterCount);
  const flushNotices = (count: number) => {
    while (pendingNotices.length && pendingNotices[0].afterCount <= count) {
      const n = pendingNotices.shift()!;
      top.push({ kind: 'notice', key: `n${n.id}`, level: n.level, text: n.text });
    }
  };
  flushNotices(0);

  messages.forEach((m, i) => {
    const base = m.uuid ?? m.local_id ?? `i${i}`;
    const parent = m.parent_tool_use_id ? tools.get(m.parent_tool_use_id) : undefined;
    const list = parent ? parent.children : top;
    if (m.role === 'user') {
      if (m.kind === 'prompt') {
        if (parent) list.push({ kind: 'meta', key: base, text: metaText(m) });
        else list.push({ kind: 'prompt', key: base, msg: m });
      } else if (m.kind === 'notice') {
        list.push({ kind: 'notice', key: base, level: 'info', text: '사용자가 응답을 중단했습니다.' });
      } else if (m.kind === 'meta') {
        const t = metaText(m);
        if (t) list.push({ kind: 'meta', key: base, text: t });
      }
    } else {
      m.blocks.forEach((b, j) => {
        const key = `${base}:${j}`;
        if (b.type === 'text') {
          if (b.text.trim()) list.push({ kind: 'text', key, text: b.text, error: j === 0 ? m.error : undefined });
        } else if (b.type === 'thinking') {
          list.push({ kind: 'thinking', key, text: b.text });
        } else if (b.type === 'tool_use') {
          const item: Extract<ChatItem, { kind: 'tool' }> = { kind: 'tool', key: b.id || key, use: b, result: results.get(b.id), children: [] };
          tools.set(b.id, item);
          list.push(item);
        } else if (b.type === 'other') {
          list.push({ kind: 'meta', key, text: b.label });
        }
      });
      if (m.error && !m.blocks.some((b) => b.type === 'text')) list.push({ kind: 'notice', key: `${base}:err`, level: 'error', text: `API 오류: ${m.error}` });
    }
    flushNotices(i + 1);
  });
  flushNotices(Number.MAX_SAFE_INTEGER);

  for (const p of partial) {
    const parent = p.parent_tool_use_id ? tools.get(p.parent_tool_use_id) : undefined;
    const list = parent ? parent.children : top;
    if (p.type === 'text') list.push({ kind: 'text', key: `p:${p.key}`, text: p.text, streaming: true });
    else if (p.type === 'thinking') list.push({ kind: 'thinking', key: `p:${p.key}`, text: p.text, streaming: true });
    else if (p.type === 'tool_use')
      list.push({
        kind: 'tool',
        key: `p:${p.key}`,
        use: { type: 'tool_use', id: p.tool_use_id ?? p.key, name: p.name ?? 'tool', input: {} },
        children: [],
        streamingInput: p.text,
      });
  }
  return top;
}
