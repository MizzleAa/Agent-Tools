// 화면 틀: 왼쪽 사이드바 + 가운데 본문(주소에 따라 대화 · 작업 폴더 · 상태 · 설정 · 관리 · 내 계정)
// 로그인 사용 모드면 AuthGate 가 로그인·비밀번호 변경 화면을 먼저 보여 준다.
import { useState } from 'react';
import { createBrowserRouter, Outlet, RouterProvider } from 'react-router-dom';
import { AccountView } from '@/features/account/AccountView';
import { AdminView } from '@/features/admin/AdminView';
import { ChatView } from '@/features/chat/ChatView';
import { ExplorerView } from '@/features/files/ExplorerView';
import { FilesView } from '@/features/files/FilesView';
import { SettingsView } from '@/features/settings/SettingsView';
import { Sidebar } from '@/features/sidebar/Sidebar';
import { StatusView } from '@/features/status/StatusView';
import { AppStateProvider } from '@/state/AppState';
import { AuthGate } from '@/state/Auth';
import { TitleProvider, useTitleValue } from '@/state/title';
import { Icon } from '@/ui/Icon';
import { UiProvider } from '@/ui/UiProvider';

function Layout() {
  const [drawer, setDrawer] = useState(false);   // 좁은 화면에서 사이드바를 서랍처럼
  const title = useTitleValue();
  return (
    <div className={`app${drawer ? ' sb-open' : ''}`}>
      <aside className="sidebar"><Sidebar onNavigate={() => setDrawer(false)} /></aside>
      <div className="scrim" onClick={() => setDrawer(false)} />
      <main className="main">
        <header className="topbar">
          <button type="button" className="icon-btn" aria-label="사이드바 열기" onClick={() => setDrawer(true)}><Icon name="menu" size={18} /></button>
          <div className="topbar-title">{title}</div>
        </header>
        <Outlet />
      </main>
    </div>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <ChatView /> },
      { path: '/c/:rootId', element: <ChatView /> },
      { path: '/cc/:ccId', element: <ChatView /> },   // CLI · Claude Code 대화
      { path: '/files', element: <FilesView /> },
      { path: '/files/:folder', element: <FilesView /> },
      { path: '/explorer', element: <ExplorerView /> },
      { path: '/explorer/:loc', element: <ExplorerView /> },
      { path: '/status', element: <StatusView /> },
      { path: '/settings', element: <SettingsView /> },
      { path: '/settings/:tab', element: <SettingsView /> },
      { path: '/settings/:tab/:name', element: <SettingsView /> },
      { path: '/admin', element: <AdminView /> },
      { path: '/admin/:tab', element: <AdminView /> },
      { path: '/account', element: <AccountView /> },
      { path: '*', element: <ChatView /> },
    ],
  },
]);

export function App() {
  return (
    <UiProvider>
      <AuthGate>
        <AppStateProvider>
          <TitleProvider>
            <RouterProvider router={router} />
          </TitleProvider>
        </AppStateProvider>
      </AuthGate>
    </UiProvider>
  );
}
