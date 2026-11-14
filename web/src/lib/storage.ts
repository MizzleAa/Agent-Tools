// 이 브라우저에만 기억해 두는 편의 값 (승인 방식, 스텝, 고른 용도 …). 못 쓰는 환경이면 조용히 넘어간다.
import { useCallback, useState } from 'react';

const PREFIX = 'agent.';

export function localGet(key: string): string | null {
  try {
    return localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

export function localSet(key: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + key, value);
  } catch {
    /* 사생활 보호 모드 등 */
  }
}

/** localStorage 에 기억되는 상태. allowed 가 있으면 그 안의 값만 받아들인다. */
export function useStored(key: string, fallback: string, allowed?: string[]): [string, (v: string) => void] {
  const [value, setValue] = useState(() => {
    const saved = localGet(key);
    return saved !== null && (!allowed || allowed.includes(saved)) ? saved : fallback;
  });
  const set = useCallback((v: string) => {
    setValue(v);
    localSet(key, v);
  }, [key]);
  return [value, set];
}
