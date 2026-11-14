// 확인 창 · 입력 창 · 넓은 창 · 알림(토스트)을 어디서나 부르게 한다 (브라우저 기본 confirm/alert 대신 Claude 스타일).
//   const ui = useUi();
//   if (await ui.confirm({ title: '지울까요?', danger: true })) …
//   const r = await ui.form({ title: '새 용도', fields: [{ name: 'name', label: '이름' }] });
//   ui.toast('저장했습니다', 'ok');  ui.error(e);  const close = ui.sheet('미리보기', <…/>);
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { errorText } from '@/api';
import { Field } from './form';
import { Icon } from './Icon';
import { Modal, Sheet } from './Modal';

export interface ConfirmOptions {
  title: string;
  message?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}
export interface FormField {
  name: string;
  label: string;
  value?: string;
  placeholder?: string;
  help?: string;
  options?: [string, string][];   // 있으면 선택 상자 [값, 이름]
  type?: 'text' | 'password' | 'number';   // 입력칸 종류 (비밀번호는 가려서)
}
export interface FormOptions {
  title: string;
  message?: string;
  fields: FormField[];
  confirmText?: string;
}
type ToastKind = 'info' | 'ok' | 'error';

interface Ui {
  confirm(o: ConfirmOptions): Promise<boolean>;
  form(o: FormOptions): Promise<Record<string, string> | null>;
  sheet(title: ReactNode, body: ReactNode): () => void;
  toast(message: string, kind?: ToastKind, ms?: number): void;
  error(e: unknown): void;
}

type Layer =
  | { id: number; kind: 'confirm'; o: ConfirmOptions; done: (v: boolean) => void }
  | { id: number; kind: 'form'; o: FormOptions; done: (v: Record<string, string> | null) => void }
  | { id: number; kind: 'sheet'; title: ReactNode; body: ReactNode };

const UiContext = createContext<Ui | null>(null);

export function useUi(): Ui {
  const ui = useContext(UiContext);
  if (!ui) throw new Error('UiProvider 밖에서 useUi 를 불렀습니다');
  return ui;
}

let nextId = 1;

export function UiProvider({ children }: { children: ReactNode }) {
  const [layers, setLayers] = useState<Layer[]>([]);
  const [toasts, setToasts] = useState<{ id: number; message: string; kind: ToastKind; leaving: boolean }[]>([]);
  const drop = useCallback((id: number) => setLayers((ls) => ls.filter((l) => l.id !== id)), []);

  const removeToast = useCallback((id: number) => {
    setToasts((ts) => ts.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 200);
  }, []);

  const ui = useMemo<Ui>(() => ({
    confirm: (o) => new Promise((resolve) => {
      const id = nextId++;
      setLayers((ls) => [...ls, { id, kind: 'confirm', o, done: (v) => { drop(id); resolve(v); } }]);
    }),
    form: (o) => new Promise((resolve) => {
      const id = nextId++;
      setLayers((ls) => [...ls, { id, kind: 'form', o, done: (v) => { drop(id); resolve(v); } }]);
    }),
    sheet: (title, body) => {
      const id = nextId++;
      setLayers((ls) => [...ls, { id, kind: 'sheet', title, body }]);
      return () => drop(id);
    },
    toast: (message, kind = 'info', ms = 4000) => {
      const id = nextId++;
      setToasts((ts) => [...ts, { id, message, kind, leaving: false }]);
      setTimeout(() => removeToast(id), ms);
    },
    error: (e) => {
      const id = nextId++;
      setToasts((ts) => [...ts, { id, message: errorText(e), kind: 'error', leaving: false }]);
      setTimeout(() => removeToast(id), 7000);
    },
  }), [drop, removeToast]);

  return (
    <UiContext.Provider value={ui}>
      {children}
      {layers.map((l) =>
        l.kind === 'confirm' ? <ConfirmDialog key={l.id} {...l.o} done={l.done} />
          : l.kind === 'form' ? <FormDialog key={l.id} {...l.o} done={l.done} />
            : <Sheet key={l.id} title={l.title} onClose={() => drop(l.id)}>{l.body}</Sheet>)}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}${t.leaving ? ' leaving' : ''}`} role={t.kind === 'error' ? 'alert' : 'status'}>
            <span className="toast-msg">{t.message}</span>
            <button type="button" className="toast-x" aria-label="닫기" onClick={() => removeToast(t.id)}><Icon name="x" size={14} /></button>
          </div>
        ))}
      </div>
    </UiContext.Provider>
  );
}

function ConfirmDialog({ title, message, confirmText = '확인', cancelText = '취소', danger, done }: ConfirmOptions & { done: (v: boolean) => void }) {
  const ok = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    ok.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); done(document.activeElement !== cancel.current); }
      else if (e.key === 'Tab') { e.preventDefault(); (document.activeElement === ok.current ? cancel : ok).current?.focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [done]);
  return (
    <Modal onClose={() => done(false)} labelledBy="confirm-title">
      <h2 className="modal-title" id="confirm-title">{title}</h2>
      {message && <div className="modal-body">{message}</div>}
      <div className="modal-actions">
        <button type="button" ref={cancel} className="btn modal-btn" onClick={() => done(false)}>{cancelText}</button>
        <button type="button" ref={ok} className={`btn modal-btn ${danger ? 'danger-solid' : 'primary'}`} onClick={() => done(true)}>{confirmText}</button>
      </div>
    </Modal>
  );
}

function FormDialog({ title, message, fields, confirmText = '확인', done }: FormOptions & { done: (v: Record<string, string> | null) => void }) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, f.value ?? (f.options ? f.options[0][0] : '')])));
  const first = useRef<HTMLInputElement & HTMLSelectElement>(null);
  useEffect(() => { first.current?.focus(); }, []);
  const submit = () => done(Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim()])));
  const set = (k: string, v: string) => setValues((vs) => ({ ...vs, [k]: v }));
  return (
    <Modal onClose={() => done(null)} labelledBy="form-title">
      <h2 className="modal-title" id="form-title">{title}</h2>
      {message && <div className="modal-body">{message}</div>}
      <form className="form-fields" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {fields.map((f, i) => (
          <Field key={f.name} label={f.label} help={f.help}>
            {f.options
              ? <select ref={i === 0 ? first : undefined} className="select" value={values[f.name]} onChange={(e) => set(f.name, e.target.value)}>
                  {f.options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </select>
              : <input ref={i === 0 ? first : undefined} type={f.type ?? "text"} autoComplete={f.type === "password" ? "new-password" : "off"} spellCheck={false} placeholder={f.placeholder}
                  value={values[f.name]} onChange={(e) => set(f.name, e.target.value)} />}
          </Field>
        ))}
        <button type="submit" hidden />
      </form>
      <div className="modal-actions">
        <button type="button" className="btn modal-btn" onClick={() => done(null)}>취소</button>
        <button type="button" className="btn modal-btn primary" onClick={submit}>{confirmText}</button>
      </div>
    </Modal>
  );
}
