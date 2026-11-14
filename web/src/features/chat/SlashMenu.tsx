// 입력창 '/' 명령 목록 (입력창 위에 뜬다). ↑↓ 고르기 · Enter 실행 · Tab 이름 채우기 · Esc 닫기. 거르는 규칙은 slash.ts.
import { type KeyboardEvent, type MouseEvent, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { expandPrompt, GROUP_LABEL, type SlashChoice, type SlashCommand, slashView, takesArg } from './slash';

/** text · setText 는 입력창 값. sendText: 요청 · CLI 명령을 보낼 때 (인자가 없는 명령은 Enter 로 고르면 바로) */
export function useSlash(text: string, setText: (v: string) => void, commands: SlashCommand[], sendText?: (t: string) => void) {
  const [active, setActive] = useState(0);
  const [closedFor, setClosedFor] = useState<string | null>(null);   // Esc 로 닫은 글 — 글이 바뀌면 다시 연다
  const view = closedFor === text ? null : slashView(text, commands);
  const count = view && view.stage !== 'hint' ? view.items.length : 0;
  useEffect(() => { setActive(0); }, [text]);

  const runCommand = (c: SlashCommand, complete: boolean) => {
    const fill = () => setText(`/${c.name} `);   // 인자를 기다린다
    if (complete || c.choices || c.needsArg) { fill(); return; }
    if (c.kind === 'cli') { if (c.hint || !sendText) fill(); else sendText(`/${c.name}`); return; }
    if (c.kind === 'prompt') { if (takesArg(c) || !sendText) fill(); else sendText(expandPrompt(c.body ?? '', '')); return; }
    setText('');
    c.run?.('');
  };
  const runChoice = (c: SlashCommand, choice: SlashChoice) => {
    if (choice.disabled) return;
    setText('');
    c.run?.(choice.value);
  };

  /** 처리했으면 true (입력창의 다른 키 처리를 건너뛴다) */
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!view || e.nativeEvent.isComposing) return false;
    if (e.key === 'Escape') { e.preventDefault(); setClosedFor(text); return true; }
    if (view.stage === 'hint') {   // 인자를 치는 중 — Enter: 요청 명령은 펼쳐 보내고, 화면 명령은 실행. CLI 명령은 그대로 보낸다
      const c = view.command;
      if (e.key !== 'Enter' || e.shiftKey) return false;
      const arg = text.slice(text.search(/\s/) + 1);
      if (c.kind === 'prompt' && sendText) { e.preventDefault(); sendText(expandPrompt(c.body ?? '', arg)); return true; }
      if (c.kind === 'web' && c.run) {
        e.preventDefault();
        if (arg.trim()) { setText(''); c.run(arg.trim()); }
        return true;
      }
      return false;
    }
    if (!count) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => (i + (e.key === 'ArrowDown' ? 1 : count - 1)) % count);
      return true;
    }
    if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
      e.preventDefault();
      const i = Math.min(active, count - 1);
      if (view.stage === 'command') runCommand(view.items[i], e.key === 'Tab');
      else runChoice(view.command, view.items[i]);
      return true;
    }
    return false;
  };

  const menu = view ? (
    <SlashMenu view={view} active={Math.min(active, Math.max(count - 1, 0))} onHover={setActive}
      onPickCommand={(c) => runCommand(c, false)} onPickChoice={runChoice} />
  ) : null;
  return { menu, onKeyDown };
}

function SlashMenu({ view, active, onHover, onPickCommand, onPickChoice }: {
  view: NonNullable<ReturnType<typeof slashView>>; active: number; onHover: (i: number) => void;
  onPickCommand: (c: SlashCommand) => void; onPickChoice: (c: SlashCommand, choice: SlashChoice) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { box.current?.querySelector('.slash-item.on')?.scrollIntoView({ block: 'nearest' }); }, [active, view]);
  const keep = (e: MouseEvent) => e.preventDefault();   // 눌러도 입력창 포커스를 잃지 않게

  if (view.stage === 'hint') {
    const c = view.command;
    return (
      <div className="slash-menu" role="note">
        <div className="slash-help"><code>/{c.name}</code>{c.hint && <span className="slash-hint">{c.hint}</span>}<span className="slash-desc">{c.desc}</span>
          <span className="slash-key">{c.kind === 'web' ? 'Enter 로 실행' : 'Enter 로 보내기'}</span></div>
      </div>
    );
  }
  if (view.stage === 'choice') {
    const c = view.command;
    return (
      <div className="slash-menu" role="listbox" ref={box} aria-label={`/${c.name}`}>
        <div className="slash-group">/{c.name} — {c.desc}</div>
        {view.items.length === 0 && <div className="slash-empty">맞는 값이 없습니다</div>}
        {view.items.map((x, i) => (
          <div key={x.value} role="option" aria-selected={i === active} aria-disabled={x.disabled}
            className={`slash-item${i === active ? ' on' : ''}${x.disabled ? ' off' : ''}`}
            onMouseDown={keep} onMouseEnter={() => onHover(i)} onClick={() => onPickChoice(c, x)}>
            <span className="slash-check">{x.current ? '✓' : ''}</span>
            <span className="slash-label">{x.label}</span>
            {x.desc && <span className="slash-desc">{x.desc}</span>}
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="slash-menu" role="listbox" ref={box} aria-label="명령">
      {view.items.map((c, i) => {
        const group = c.group ?? GROUP_LABEL[c.kind];
        const prev = view.items[i - 1];
        const head = !prev || (prev.group ?? GROUP_LABEL[prev.kind]) !== group;
        return (
          <div key={`${c.kind}:${c.name}`}>
            {head && <div className="slash-group">{group}</div>}
            <div role="option" aria-selected={i === active} className={`slash-item${i === active ? ' on' : ''}`} title={c.title ?? c.desc}
              onMouseDown={keep} onMouseEnter={() => onHover(i)} onClick={() => onPickCommand(c)}>
              <span className="slash-name">/{c.name}</span>
              {c.hint && <span className="slash-hint">{c.hint}</span>}
              <span className="slash-desc">{c.desc}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
