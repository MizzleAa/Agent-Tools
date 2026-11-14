// 설정·상태 화면에서 같이 쓰는 칸들
import { type ReactNode, type RefObject, type TextareaHTMLAttributes, useLayoutEffect, useRef } from 'react';
import { sizeText } from '@/lib/format';

export function Field({ label, help, children }: { label: ReactNode; help?: ReactNode; children: ReactNode }) {
  return (
    <label className="set-field">
      <span className="set-label">{label}</span>
      {children}
      {help && <span className="muted small">{help}</span>}
    </label>
  );
}

/** 카드 한 장. title 이 글자면 제목줄을 만들고, 요소면 그대로 제목줄로 쓴다 */
export function Section({ title, help, right, children, className = '' }: {
  title?: ReactNode; help?: ReactNode; right?: ReactNode; children?: ReactNode; className?: string;
}) {
  return (
    <div className={`card wide set-card ${className}`}>
      {(title || right) && (
        <div className="card-head">
          {typeof title === 'string' ? <span className="title">{title}</span> : title}
          {right && <><span className="spacer" />{right}</>}
        </div>
      )}
      {help && <div className="muted small set-help">{help}</div>}
      {children}
    </div>
  );
}

export function Badge({ kind = '', children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function Chip({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={`chip ${className}`}>{children}</span>;
}

/** 내용에 맞춰 높이가 늘어나는 입력칸. Tab = 공백 두 칸 */
export function Editor({ value, onChange, max = 640, rows = 6, inputRef, ...rest }: {
  value: string; onChange: (v: string) => void; max?: number; rows?: number;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
} & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'onChange'>) {
  const own = useRef<HTMLTextAreaElement>(null);
  const ref = inputRef ?? own;
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight + 2, max)}px`;
  }, [value, max]);
  return (
    <textarea
      ref={ref} className="editor" rows={rows} spellCheck={false} value={value} {...rest}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Tab' && !e.shiftKey && !e.nativeEvent.isComposing) {
          e.preventDefault();
          const ta = e.currentTarget;
          ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end');
          onChange(ta.value);
        }
      }}
    />
  );
}

/** 입력칸 커서 자리에 글자를 넣는다 ({{변수}} 버튼) */
export function insertAt(ta: HTMLTextAreaElement | null, text: string, onChange: (v: string) => void) {
  if (!ta) return;
  ta.focus();
  ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
  onChange(ta.value);
}

export function SizeLine({ text }: { text: string }) {
  return <span className="muted small">{sizeText(text)}</span>;
}

export function Toggle({ checked, onChange, label, help }: { checked: boolean; onChange: (v: boolean) => void; label: string; help?: string }) {
  return (
    <label className="set-toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span><b>{label}</b>{help && <span className="muted small">{help}</span>}</span>
    </label>
  );
}
