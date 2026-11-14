// 화면에 보이는 서비스 이름 (관리 › 접속 설정에서 바꾼다). 로그인 상태를 읽으면 state/Auth.tsx 가 채운다.
export const brand = { name: 'AI Agent' };

export function pageTitle(title: string): string {
  return title ? `${title} · ${brand.name}` : brand.name;
}
