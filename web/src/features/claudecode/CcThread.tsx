// Claude Code 대화 내용 (대화 화면의 .thread 모양 그대로): 사용자 말풍선 → 그 응답(생각 · 도구 카드 · 답변)
import { memo, useCallback, useMemo, type ReactNode } from 'react';
import { CopyButton } from '@/ui/CopyButton';
import { Icon, Spinner } from '@/ui/Icon';
import { Markdown } from '@/ui/Markdown';
import { mergedMessages } from './chat';
import { buildItems, type ChatItem } from './items';
import { loadOlder } from './actions';
import { chatOf, useCc } from './store';
import { ToolCard } from './ToolCard';
import type { NormMessage } from './types';

const Prompt = memo(function Prompt({ msg, onRetry }: { msg: NormMessage; onRetry: (t: string) => void }) {
  const text = msg.blocks.filter((b) => b.type === 'text').map((b) => (b as { text: string }).text).join('\n');
  const images = msg.blocks.filter((b) => b.type === 'image') as { media_type: string; data?: string }[];
  return (
    <div className={`user-msg${msg.uuid ? '' : ' cc-pending'}`}>
      <div className="user-bubble">
        {text}
        {images.length > 0 && <div className="cc-images">{images.map((im, i) => (im.data ? <img key={i} src={`data:${im.media_type};base64,${im.data}`} alt="첨부 이미지" /> : null))}</div>}
        {msg.failed && <div className="cc-failed">전달되지 않았을 수 있습니다 <button type="button" className="btn link" onClick={() => onRetry(text)}>다시 보내기</button></div>}
      </div>
    </div>
  );
});

export function CcThread({ sessionId, cwd, onRetry }: { sessionId: string; cwd: string; onRetry: (text: string) => void }) {
  const chat = useCc((s) => chatOf(s, sessionId));
  const showThinking = useCc((s) => s.settings?.show_thinking ?? true);
  const messages = useMemo(() => mergedMessages(chat), [chat]);
  const items = useMemo(() => buildItems(messages, chat.partial, chat.notices), [messages, chat.partial, chat.notices]);
  const waitingIds = useMemo(() => new Set(chat.pending.map((p) => p.tool_use_id).filter(Boolean) as string[]), [chat.pending]);
  const running = chat.turn !== null;

  const render = useCallback((list: ChatItem[]): ReactNode => list.map((it) => {
    switch (it.kind) {
      case 'prompt': return <Prompt key={it.key} msg={it.msg} onRetry={onRetry} />;
      case 'text':
        return (
          <div key={it.key} className={`answer${it.streaming ? ' cc-streaming' : ''}`}>
            <Markdown text={it.text} />
            {it.error && <div className="err-text small">API 오류: {it.error}</div>}
            {!it.streaming && <div className="answer-foot"><CopyButton text={it.text} /></div>}
          </div>
        );
      case 'thinking':
        if (!showThinking || (!it.streaming && !it.text.trim())) return null;
        return it.text.trim() ? (
          <details key={it.key} className="thinking"><summary><Icon name="chevron" size={14} className="chev" />{it.streaming ? '생각하는 중…' : '생각 과정'}</summary>
            <div className="thinking-body">{it.text}</div></details>
        ) : <div key={it.key} className="muted small cc-think"><Spinner /> 생각하는 중…</div>;
      case 'tool':
        return <ToolCard key={it.key} item={it} cwd={cwd} running={running} waiting={waitingIds.has(it.use.id)} renderChildren={render} />;
      case 'notice':
        return <div key={it.key} className={`notice${it.level === 'error' ? ' error' : ''}`}><span className="msg">{it.text}</span></div>;
      case 'meta':
        return <div key={it.key} className="loop-note">{it.text}</div>;
    }
  }), [cwd, running, waitingIds, showThinking, onRetry]);

  // 사용자 말풍선마다 그 뒤 응답을 .assistant 로 묶는다 (대화 화면과 같은 간격)
  const groups: { prompt: ChatItem | null; rest: ChatItem[] }[] = [];
  for (const it of items) {
    if (it.kind === 'prompt' || groups.length === 0) groups.push({ prompt: it.kind === 'prompt' ? it : null, rest: it.kind === 'prompt' ? [] : [it] });
    else groups[groups.length - 1].rest.push(it);
  }
  return (
    <div className="thread">
      {chat.historyStart > 0 && <button type="button" className="btn cc-older" onClick={() => loadOlder(sessionId)}>이전 대화 더 보기 ({chat.historyStart}개)</button>}
      {chat.historyError && <div className="notice error"><span className="msg">{chat.historyError}</span></div>}
      {!chat.historyLoaded && <div className="muted small"><Spinner /> 기록을 불러오는 중…</div>}
      {groups.map((g, i) => (
        <div key={g.prompt?.key ?? `g${i}`} className="cc-turn">
          {g.prompt && render([g.prompt])}
          {(g.rest.length > 0 || (running && i === groups.length - 1)) && (
            <div className="assistant">
              {render(g.rest)}
              {running && i === groups.length - 1 && chat.partial.length === 0 && (
                <div className="muted small cc-working"><Spinner /><span>{
                  chat.pending.length ? '승인을 기다리는 중'
                    : chat.turn?.statusText === 'retrying' ? `Claude API 에 연결하지 못해 다시 시도하는 중… (${chat.turn.retry?.attempt ?? ''}/${chat.turn.retry?.max_retries ?? ''}) — Esc 로 중단`
                      : chat.turn?.statusText === 'stalled' ? '응답이 늦어지고 있습니다 (네트워크 확인) — Esc 로 중단'
                        : chat.turn?.statusText === 'compacting' ? '대화를 정리하는 중…' : '작업 중…'}</span></div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
