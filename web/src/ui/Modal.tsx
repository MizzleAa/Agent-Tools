// 대화상자 바탕: 화면을 덮는 반투명 막 + 가운데 상자. Esc · 바깥 클릭으로 닫힌다.
import { type ReactNode, useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';

export function Modal({ onClose, wide = false, labelledBy, children }: {
  onClose: () => void; wide?: boolean; labelledBy?: string; children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const raf = requestAnimationFrame(() => setOpen(true));
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); closeRef.current(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => { cancelAnimationFrame(raf); document.removeEventListener('keydown', onKey, true); };
  }, []);
  return (
    <div className={`modal-overlay${open ? ' open' : ''}`} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={labelledBy}>
        {children}
      </div>
    </div>
  );
}

/** 제목 + 내용 + 닫기만 있는 넓은 창 (미리보기, 비교) */
export function Sheet({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  return (
    <Modal onClose={onClose} wide labelledBy="sheet-title">
      <div className="sheet-head">
        <h2 className="modal-title" id="sheet-title">{title}</h2>
        <button type="button" className="icon-btn" aria-label="닫기" onClick={onClose}><Icon name="x" /></button>
      </div>
      <div className="sheet-body">{children}</div>
    </Modal>
  );
}
