// 남은 공통 탭 (개발 규격 · 변경 기록 · 안내) 고르기
import { GuideTab, HistoryTab, StandardsTab } from './FileTabs';

export function FileTabsGuide({ tab, name }: { tab: string; name?: string }) {
  if (tab === 'standards') return <StandardsTab selected={name ? decodeURIComponent(name) : undefined} />;
  if (tab === 'history') return <HistoryTab />;
  return <GuideTab />;
}
