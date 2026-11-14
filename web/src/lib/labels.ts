// 화면에 보이는 한국어 이름표

export const PERM: Record<string, string> = {
  read: '읽기', write: '쓰기', exec: '실행', send: 'API 변경', admin: '설정 변경', unknown: '알 수 없음',
};
export const PERM_ORDER: Record<string, number> = { read: 0, write: 1, exec: 2, send: 3, admin: 4 };

export const CRAWL: Record<string, string> = { done: '완료', cancelled: '중지됨', error: '오류', running: '수집 중' };

export const RUNTIME: Record<string, string> = {
  ok: '준비됨', loading: '적재 중', down: '꺼짐', occupied: '포트를 다른 프로그램이 사용 중',
};

export const STATUS: Record<string, string> = {
  done: '완료', partial: '부분 완료', failed: '실패', no_action: '실행 안 함', max_steps: '스텝 한도',
  skipped: '건너뜀', reused: '이전 결과', timeout: '시간 초과', loop: '반복 감지로 멈춤', cancelled: '중지됨',
  error: '오류', running: '실행 중', interrupted: '중단됨',
};

export const TOOL_LABEL: Record<string, string> = {
  list_dir: '폴더 보기', read_file: '파일 읽기', search_files: '파일 찾기', write_file: '파일 저장',
  run_command: '명령 실행', run_python: '파이썬 실행', web_search: '웹 검색', fetch_url: '페이지 읽기',
  search_intranet: '사내 검색', api_get: 'API 조회', api_send: 'API 전송', find_endpoint: 'API 경로 찾기',
  add_intranet_site: '사이트 등록', crawl_site: '다시 수집', add_api: 'API 등록', allow_host: '내부 주소 허용',
  read_standard: '규격 읽기', show_images: '이미지 표시',
};

export const SUGGEST: Record<string, string[]> = {
  general: ['workspace 폴더에 뭐가 있는지 정리해줘', '최근에 바뀐 파일 5개 알려줘'],
  coder: ['numbers.txt 숫자 합계를 파이썬으로 계산해줘', '간단한 PyQt5 사진 뷰어 만들어줘'],
  researcher: ['llama.cpp 최근 변경점 조사해줘', 'Qwen3.6 특징을 출처와 함께 정리해줘'],
  intranet: ['사내 위키에서 설비 점검 주기 찾아줘', 'http://wiki.intra/ 추가해줘'],
  'wf:build-review': ['할 일 목록을 관리하는 파이썬 CLI 만들어줘'],
};

export const FILE_STATE: Record<string, [string, string] | null> = {
  ready: null, downloading: ['warn', '내려받는 중'], missing: ['fail', '파일 없음'],
};
