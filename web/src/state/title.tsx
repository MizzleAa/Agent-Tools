// 좁은 화면 위쪽 막대의 제목 (대화 제목, '설정 · 프롬프트' …)
import { createContext, type ReactNode, useContext, useEffect, useState } from 'react';
import { pageTitle } from '@/lib/brand';

const Ctx = createContext<{ title: string; setTitle: (t: string) => void }>({ title: '', setTitle: () => {} });

export function TitleProvider({ children }: { children: ReactNode }) {
  const [title, setTitle] = useState('');
  return <Ctx.Provider value={{ title, setTitle }}>{children}</Ctx.Provider>;
}

export const useTitleValue = () => useContext(Ctx).title;

export function useTitle(title: string) {
  const { setTitle } = useContext(Ctx);
  useEffect(() => {
    setTitle(title);
    document.title = pageTitle(title);
  }, [title, setTitle]);
}
