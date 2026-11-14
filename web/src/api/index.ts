// 자원별 API 함수. 화면 코드는 fetch 대신 이것만 쓴다.
import { http, unwrap, type Schemas as S } from './client';

export type { Schemas } from './client';
export { ApiError, AUTH_EVENT, errorText, TOKEN } from './client';

export type RunEvent = S['RunDetail']['events'][number];
export type EventOf<T extends RunEvent['type']> = Extract<RunEvent, { type: T }>;
export type AutoMode = S['AutoApprove']['mode'];

const p = <T extends Record<string, string>>(path: T) => ({ params: { path } });

export const api = {
  runs: {
    start: (body: S['RunCreate']) => unwrap(http.POST('/api/v1/runs', { body })),
    list: (limit = 200) => unwrap(http.GET('/api/v1/runs', { params: { query: { limit } } })),
    get: (run_id: string) => unwrap(http.GET('/api/v1/runs/{run_id}', p({ run_id }))),
    /** 대화 하나의 실행들 (처음 것부터). 관리자는 남의 대화도 연다 */
    thread: (root_id: string) => unwrap(http.GET('/api/v1/threads/{root_id}', p({ root_id }))),
    approve: (run_id: string, call_id: string, approved: boolean) =>
      unwrap(http.POST('/api/v1/runs/{run_id}/approvals', { ...p({ run_id }), body: { call_id, approved } })),
    autoApprove: (run_id: string, mode: AutoMode) =>
      unwrap(http.PUT('/api/v1/runs/{run_id}/auto-approve', { ...p({ run_id }), body: { mode } })),
    cancel: (run_id: string) => unwrap(http.POST('/api/v1/runs/{run_id}/cancel', p({ run_id }))),
    openFolder: (run_id: string) => unwrap(http.POST('/api/v1/runs/{run_id}/open-folder', p({ run_id }))),
    /** 실시간 이벤트 (EventSource 로 연다) */
    eventsUrl: (run_id: string, since = 0) => `/api/v1/runs/${encodeURIComponent(run_id)}/events?since=${since}`,
  },
  threads: {
    remove: (root_id: string) => unwrap(http.DELETE('/api/v1/threads/{root_id}', p({ root_id }))),
  },
  auth: {
    me: () => unwrap(http.GET('/api/v1/auth/me')),
    login: (username: string, password: string) => unwrap(http.POST('/api/v1/auth/login', { body: { username, password } })),
    logout: () => unwrap(http.POST('/api/v1/auth/logout')),
    changePassword: (current_password: string, new_password: string) =>
      unwrap(http.POST('/api/v1/auth/password', { body: { current_password, new_password } })),
    requestReset: (body: S['ResetRequestBody']) => unwrap(http.POST('/api/v1/auth/reset-requests', { body })),
    setup: (body: S['SetupBody']) => unwrap(http.POST('/api/v1/auth/setup', { body })),
  },
  me: {
    apiAccounts: () => unwrap(http.GET('/api/v1/me/api-accounts')),
    // 값은 저장만 한다 — 서버가 어디로도 돌려주지 않는다
    setApiAccount: (name: string, body: S['MyApiCredentials']) => unwrap(http.PUT('/api/v1/me/api-accounts/{name}', { ...p({ name }), body })),
    clearApiAccount: (name: string) => unwrap(http.DELETE('/api/v1/me/api-accounts/{name}', p({ name }))),
    checkApiAccount: (name: string) => unwrap(http.POST('/api/v1/me/api-accounts/{name}/check', p({ name }))),
  },
  admin: {
    /** 서버 PC 폴더 목록 — 입력창 '작업 위치' 고르기 (로컬 에이전트·CLI 공통) */
    dirs: (path?: string) => unwrap(http.GET('/api/v1/admin/fs/dirs', { params: { query: { path: path ?? null } } })),
    companies: () => unwrap(http.GET('/api/v1/admin/companies')),
    createCompany: (body: S['CompanyWrite']) => unwrap(http.POST('/api/v1/admin/companies', { body })),
    updateCompany: (company_id: string, body: S['CompanyWrite']) =>
      unwrap(http.PUT('/api/v1/admin/companies/{company_id}', { ...p({ company_id }), body })),
    deleteCompany: (company_id: string) => unwrap(http.DELETE('/api/v1/admin/companies/{company_id}', p({ company_id }))),
    users: () => unwrap(http.GET('/api/v1/admin/users')),
    createUser: (body: S['UserCreate']) => unwrap(http.POST('/api/v1/admin/users', { body })),
    updateUser: (user_id: string, body: S['UserUpdate']) => unwrap(http.PUT('/api/v1/admin/users/{user_id}', { ...p({ user_id }), body })),
    deleteUser: (user_id: string) => unwrap(http.DELETE('/api/v1/admin/users/{user_id}', p({ user_id }))),
    resetPassword: (user_id: string) => unwrap(http.POST('/api/v1/admin/users/{user_id}/reset-password', p({ user_id }))),
    resetRequests: (pending = false) => unwrap(http.GET('/api/v1/admin/reset-requests', { params: { query: { pending } } })),
    dismissRequest: (request_id: string) =>
      unwrap(http.POST('/api/v1/admin/reset-requests/{request_id}/dismiss', p({ request_id }))),
    access: () => unwrap(http.GET('/api/v1/admin/access')),
    saveAccess: (body: S['AccessSettings']) => unwrap(http.PUT('/api/v1/admin/access', { body })),
    providers: () => unwrap(http.GET('/api/v1/admin/providers')),
    addProvider: (body: S['ProviderCreate']) => unwrap(http.POST('/api/v1/admin/providers', { body })),
    updateProvider: (provider_id: string, body: S['ProviderUpdate']) =>
      unwrap(http.PUT('/api/v1/admin/providers/{provider_id}', { ...p({ provider_id }), body })),
    checkProvider: (provider_id: string) => unwrap(http.POST('/api/v1/admin/providers/{provider_id}/check', p({ provider_id }))),
    // Claude 회사 계정 로그인 — 서버가 공식 CLI 를 대신 띄운다 (서버 PC: 브라우저 · 다른 PC: 주소 + 코드)
    startLogin: (provider_id: string) => unwrap(http.POST('/api/v1/admin/providers/{provider_id}/login', p({ provider_id }))),
    loginStatus: (provider_id: string) => unwrap(http.GET('/api/v1/admin/providers/{provider_id}/login', p({ provider_id }))),
    loginCode: (provider_id: string, code: string) =>
      unwrap(http.POST('/api/v1/admin/providers/{provider_id}/login/code', { ...p({ provider_id }), body: { code } })),
    cancelLogin: (provider_id: string) => unwrap(http.DELETE('/api/v1/admin/providers/{provider_id}/login', p({ provider_id }))),
    deleteProvider: (provider_id: string) => unwrap(http.DELETE('/api/v1/admin/providers/{provider_id}', p({ provider_id }))),
    runs: (query: { user_id?: string; company_id?: string; provider?: string; connection?: string; model?: string; status?: string;
      since?: string; until?: string; limit?: number; offset?: number }) => unwrap(http.GET('/api/v1/admin/runs', { params: { query } })),
    usage: (query: { group?: 'user_id' | 'company_id' | 'connection' | 'model' | 'provider'; since?: string; until?: string }) =>
      unwrap(http.GET('/api/v1/admin/usage', { params: { query } })),
  },
  harness: {   // scope: '' = 공통 하네스, 그 밖에는 회사·프로젝트 하네스 이름
    scopes: () => unwrap(http.GET('/api/v1/harness/scopes')),
    createScope: (body: S['ScopeCreate']) => unwrap(http.POST('/api/v1/harness/scopes', { body })),
    updateScope: (id: string, body: S['ScopeUpdate']) => unwrap(http.PUT('/api/v1/harness/scopes/{scope_id}', { ...p({ scope_id: id }), body })),
    removeScope: (id: string) => unwrap(http.DELETE('/api/v1/harness/scopes/{scope_id}', p({ scope_id: id }))),
    tree: (scope = '') => unwrap(http.GET('/api/v1/harness', { params: { query: { scope } } })),
    file: (path: string, scope = '') => unwrap(http.GET('/api/v1/harness/file', { params: { query: { path, scope } } })),
    save: (path: string, text: string, scope = '') => unwrap(http.PUT('/api/v1/harness/file', { params: { query: { scope } }, body: { path, text } })),
    remove: (path: string, scope = '') => unwrap(http.DELETE('/api/v1/harness/file', { params: { query: { path, scope } } })),
    mkdir: (path: string, scope = '') => unwrap(http.POST('/api/v1/harness/folders', { params: { query: { scope } }, body: { path } })),
    setCompanies: (path: string, companies: string[], scope = '') =>
      unwrap(http.PUT('/api/v1/harness/folders/companies', { params: { query: { scope } }, body: { path, companies } })),
    rename: (src: string, dst: string, scope = '') => unwrap(http.POST('/api/v1/harness/rename', { params: { query: { scope } }, body: { src, dst } })),
    upload: (folder: string, files: { name: string; data: string }[], scope = '') =>
      unwrap(http.POST('/api/v1/harness/upload', { params: { query: { scope } }, body: { folder, files } })),
    exportRoles: () => unwrap(http.POST('/api/v1/harness/export-roles')),
  },
  catalog: {
    profiles: () => unwrap(http.GET('/api/v1/profiles')),
    harnesses: () => unwrap(http.GET('/api/v1/harnesses')),
    workflows: () => unwrap(http.GET('/api/v1/workflows')),
  },
  system: {
    status: () => unwrap(http.GET('/api/v1/status')),
    startRuntime: () => unwrap(http.POST('/api/v1/runtime/start')),
    stopRuntime: () => unwrap(http.POST('/api/v1/runtime/stop')),
    selectModel: (name: string) => unwrap(http.PUT('/api/v1/models/active', { body: { name } })),
  },
  intranet: {
    addSource: (body: S['SourceCreate']) => unwrap(http.POST('/api/v1/intranet/sources', { body })),
    removeSource: (name: string) => unwrap(http.DELETE('/api/v1/intranet/sources/{name}', p({ name }))),
    crawl: (source = '') => unwrap(http.POST('/api/v1/intranet/crawls', { body: { source } })),
    cancelCrawl: (source = '') => unwrap(http.POST('/api/v1/intranet/crawls/cancel', { body: { source } })),
  },
  apis: {
    add: (body: S['ApiCreate']) => unwrap(http.POST('/api/v1/apis', { body })),
    remove: (name: string) => unwrap(http.DELETE('/api/v1/apis/{name}', p({ name }))),
    refresh: (name: string) => unwrap(http.POST('/api/v1/apis/{name}/refresh', p({ name }))),
    writeAccess: (name: string, allow: boolean) =>
      unwrap(http.PUT('/api/v1/apis/{name}/write-access', { ...p({ name }), body: { allow } })),
    setAuth: (name: string, body: S['ApiAuthUpdate']) => unwrap(http.PUT('/api/v1/apis/{name}/auth', { ...p({ name }), body })),
    // 값은 저장만 한다 — 서버가 어디로도 돌려주지 않는다
    credentials: (name: string, body: S['ApiCredentials']) =>
      unwrap(http.PUT('/api/v1/apis/{name}/credentials', { ...p({ name }), body })),
    loginCheck: (name: string) => unwrap(http.POST('/api/v1/apis/{name}/login-check', p({ name }))),
  },
  hosts: {
    add: (rule: string) => unwrap(http.POST('/api/v1/hosts', { body: { rule } })),
    remove: (rule: string) => unwrap(http.DELETE('/api/v1/hosts', { params: { query: { rule } } })),
  },
  secrets: {
    put: (name: string, value: string) => unwrap(http.PUT('/api/v1/secrets/{name}', { ...p({ name }), body: { value } })),
  },
  locations: {
    list: () => unwrap(http.GET('/api/v1/locations')),
    add: (path: string) => unwrap(http.POST('/api/v1/locations', { body: { path } })),
    remove: (loc_id: string) => unwrap(http.DELETE('/api/v1/locations/{loc_id}', p({ loc_id }))),
    // 계정은 저장만 한다 — 서버가 어디로도 돌려주지 않는다
    setAccount: (loc_id: string, body: S['ShareAccount']) =>
      unwrap(http.PUT('/api/v1/locations/{loc_id}/account', { ...p({ loc_id }), body })),
    clearAccount: (loc_id: string) => unwrap(http.DELETE('/api/v1/locations/{loc_id}/account', p({ loc_id }))),
    folder: (loc_id: string, query: { path?: string; offset?: number; limit?: number; sort?: 'name' | 'size' | 'modified' | 'type';
      order?: 'asc' | 'desc'; q?: string }) =>
      unwrap(http.GET('/api/v1/locations/{loc_id}/list', { params: { path: { loc_id }, query } })),
    file: (loc_id: string, path: string) =>
      unwrap(http.GET('/api/v1/locations/{loc_id}/file', { params: { path: { loc_id }, query: { path } } })),
    /** 이미지 미리보기 주소 (위치 id·상대 경로만 — 인증 정보 없음) */
    rawUrl: (loc_id: string, path: string) => `/api/v1/locations/${encodeURIComponent(loc_id)}/raw?path=${encodeURIComponent(path)}`,
  },
  workspace: {
    folders: () => unwrap(http.GET('/api/v1/workspace/folders')),
    tree: (name: string) => unwrap(http.GET('/api/v1/workspace/folders/{name}/tree', p({ name }))),
    file: (name: string, path: string) =>
      unwrap(http.GET('/api/v1/workspace/folders/{name}/file', { params: { path: { name }, query: { path } } })),
  },
  settings: {
    get: () => unwrap(http.GET('/api/v1/settings')),
    preview: (body: S['Profile-Input']) => unwrap(http.POST('/api/v1/settings/preview', { body })),
    profiles: {
      create: (body: S['Profile-Input']) => unwrap(http.POST('/api/v1/settings/profiles', { body })),
      update: (body: S['Profile-Input']) => unwrap(http.PUT('/api/v1/settings/profiles/{name}', { ...p({ name: body.name }), body })),
      remove: (name: string) => unwrap(http.DELETE('/api/v1/settings/profiles/{name}', p({ name }))),
      reset: (name: string) => unwrap(http.POST('/api/v1/settings/profiles/{name}/reset', p({ name }))),
    },
    workflows: {
      create: (body: S['Workflow-Input']) => unwrap(http.POST('/api/v1/settings/workflows', { body })),
      update: (body: S['Workflow-Input']) => unwrap(http.PUT('/api/v1/settings/workflows/{name}', { ...p({ name: body.name }), body })),
      remove: (name: string) => unwrap(http.DELETE('/api/v1/settings/workflows/{name}', p({ name }))),
      reset: (name: string) => unwrap(http.POST('/api/v1/settings/workflows/{name}/reset', p({ name }))),
    },
    blocks: {
      put: (block_id: string, body: S['BlockWrite']) => unwrap(http.PUT('/api/v1/settings/blocks/{block_id}', { ...p({ block_id }), body })),
      reset: (block_id: string) => unwrap(http.DELETE('/api/v1/settings/blocks/{block_id}', p({ block_id }))),
    },
    messages: {
      put: (key: string, text: string) => unwrap(http.PUT('/api/v1/settings/messages/{key}', { ...p({ key }), body: { text } })),
      reset: (key: string) => unwrap(http.DELETE('/api/v1/settings/messages/{key}', p({ key }))),
    },
    tools: {
      put: (name: string, text: string) => unwrap(http.PUT('/api/v1/settings/tools/{name}', { ...p({ name }), body: { text } })),
      reset: (name: string) => unwrap(http.DELETE('/api/v1/settings/tools/{name}', p({ name }))),
    },
    variables: {
      put: (name: string, body: S['VariableWrite']) => unwrap(http.PUT('/api/v1/settings/variables/{name}', { ...p({ name }), body })),
      reset: (name: string) => unwrap(http.DELETE('/api/v1/settings/variables/{name}', p({ name }))),
    },
    models: {
      create: (body: S['ModelCreate']) => unwrap(http.POST('/api/v1/settings/models', { body })),
      update: (name: string, body: S['ModelUpdate']) => unwrap(http.PUT('/api/v1/settings/models/{name}', { ...p({ name }), body })),
      remove: (name: string) => unwrap(http.DELETE('/api/v1/settings/models/{name}', p({ name }))),
      reset: (name: string) => unwrap(http.POST('/api/v1/settings/models/{name}/reset', p({ name }))),
    },
    params: {
      put: (section: string, values: Record<string, unknown>) =>
        unwrap(http.PUT('/api/v1/settings/params/{section}', { ...p({ section }), body: { values } })),
    },
    standards: {
      get: (name: string) => unwrap(http.GET('/api/v1/settings/standards/{name}', p({ name }))),
      put: (name: string, text: string) => unwrap(http.PUT('/api/v1/settings/standards/{name}', { ...p({ name }), body: { text } })),
    },
    history: {
      list: () => unwrap(http.GET('/api/v1/settings/history')),
      get: (entry_id: string) => unwrap(http.GET('/api/v1/settings/history/{entry_id}', p({ entry_id }))),
      restore: (entry_id: string) => unwrap(http.POST('/api/v1/settings/history/{entry_id}/restore', p({ entry_id }))),
    },
  },
};
