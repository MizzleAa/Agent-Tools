/// <reference types="vitest/config" />
// 개발: 관리 서버(python -m server)를 AGENT_TOKEN 환경변수와 함께 켠 뒤 `npm run dev` → http://127.0.0.1:5173
//       /api 요청은 관리 서버로 넘긴다. 토큰은 같은 AGENT_TOKEN 값을 index.html 에 넣는다.
// 배포: `npm run build` → dist/ 를 관리 서버가 그대로 내보낸다 (토큰은 서버가 index.html 에 넣는다).
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

const API = process.env.AGENT_API ?? 'http://127.0.0.1:8765';

function devToken(): Plugin {
  return {
    name: 'agent-dev-token',
    apply: 'serve',
    transformIndexHtml: (html) => html.replace('__AGENT_TOKEN__', process.env.AGENT_TOKEN ?? '').replace('__APP_NAME__', 'AI Agent'),
  };
}

export default defineConfig({
  plugins: [react(), devToken()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/api': { target: API, changeOrigin: true } },   // changeOrigin: 관리 서버의 Host 검사를 통과하게
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { environment: 'node', include: ['src/**/*.test.{ts,tsx}'] },
});
