import { useState } from 'react';
import { Icon } from './Icon';

export function CopyButton({ text, label = '복사' }: { text: string | (() => string); label?: string }) {
  const [shown, setShown] = useState(label);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(typeof text === 'function' ? text() : text);
      setShown('복사됨');
    } catch {
      setShown('복사 실패');
    }
    setTimeout(() => setShown(label), 1500);
  };
  return (
    <button type="button" className="mini-btn" title="복사" onClick={copy}>
      <Icon name="copy" size={14} /><span>{shown}</span>
    </button>
  );
}
