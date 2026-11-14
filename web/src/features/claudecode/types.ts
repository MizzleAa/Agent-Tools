// WebSocket 이벤트와 정규화된 대화 메시지 모양 (백엔드 ccgui/transcript.py · session_manager.py 와 맞춘다).
import type { Project, Session } from './api';

export type TextBlock = { type: 'text'; text: string };
export type ThinkingBlock = { type: 'thinking'; text: string };
export type ToolUseBlock = { type: 'tool_use'; id: string; name: string; input: Record<string, unknown> };
export type ImageBlock = { type: 'image'; media_type: string; data?: string; omitted?: boolean };
export type ToolResultBlock = {
  type: 'tool_result';
  tool_use_id: string;
  is_error: boolean;
  content: string;
  truncated?: boolean;
  images?: ImageBlock[];
};
export type OtherBlock = { type: 'other'; label: string };
export type Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock | OtherBlock;

export type MessageKind = 'prompt' | 'assistant' | 'tool_results' | 'notice' | 'meta';

export interface NormMessage {
  uuid: string | null;
  local_id?: string;
  role: 'user' | 'assistant';
  kind: MessageKind;
  blocks: Block[];
  model?: string | null;
  message_id?: string | null;
  parent_tool_use_id?: string | null;
  error?: string;
  failed?: boolean; // 화면 전용: 보내지 못한 메시지
}

export interface PartialBlock {
  key: string;
  message_id: string | null;
  index: number;
  type: 'text' | 'thinking' | 'tool_use' | string;
  text: string;
  name?: string | null;
  tool_use_id?: string | null;
  parent_tool_use_id?: string | null;
}

export interface PermissionSuggestion {
  type: string;
  destination?: string;
  mode?: string;
  behavior?: string;
  rules?: { toolName: string; ruleContent?: string }[];
  directories?: string[];
}

export interface PermissionReq {
  id: string;
  kind: 'permission' | 'question' | 'plan';
  tool_name: string;
  tool_use_id: string | null;
  input: Record<string, unknown>;
  suggestions: PermissionSuggestion[];
  decision_reason?: string | null;
  blocked_path?: string | null;
  created_at: number;
}

export interface TurnResult {
  turn_id: string | null;
  subtype: string;
  is_error: boolean;
  interrupted: boolean;
  duration_ms: number;
  num_turns: number;
  total_cost_usd: number | null;
  turn_cost_usd?: number | null;
  usage?: Record<string, number> | null;
  result: string | null;
  errors?: string[] | null;
  terminal_reason?: string | null;
  permission_denials: number;
}

export interface InitInfo {
  model?: string;
  cwd?: string;
  permission_mode?: string;
  claude_code_version?: string;
  tools?: number;
  mcp_servers?: { name: string; status: string }[];
  api_key_source?: string;
}

export interface LiveTurn {
  turn_id: string;
  started_at: number;
  prompt: NormMessage;
  messages: NormMessage[];
  partial: PartialBlock[];
  status_text: string | null;
  interrupted: boolean;
}

export interface ProcessInfo {
  session_id: string;
  pid: number | null;
  status: string;
  cli_path: string;
  cli_version: string | null;
  exit_code: number | null;
  registry_id: number | null;
}

export interface RateLimit {
  status: string;
  utilization: number | null;
  type: string | null;
  resets_at: number | null;
}

export type ServerEvent =
  | { type: 'hello'; session_id: null; data: { client_id: number } }
  | { type: 'pong' | 'resync'; session_id: null; data: Record<string, never> }
  | { type: 'snapshot'; session_id: string; data: { session: Session | null; process: ProcessInfo | null; pending: PermissionReq[]; live_turn: LiveTurn | null; init: InitInfo | null } }
  | { type: 'session.updated'; session_id: string; data: { session: Session } }
  | { type: 'session.deleted'; session_id: string; data: { session_id: string } }
  | { type: 'project.updated'; session_id: null; data: { project: Project } }
  | { type: 'project.deleted'; session_id: null; data: { project_id: string } }
  | { type: 'process.updated'; session_id: string; data: { process: ProcessInfo } }
  | { type: 'rate_limit'; session_id: string; data: { rate_limit: RateLimit } }
  | { type: 'turn.started'; session_id: string; data: { turn_id: string; prompt: NormMessage } }
  | { type: 'turn.status'; session_id: string; data: { status: string | null; retry?: { attempt: number; max_retries: number; delay_ms: number; error_status: number | null } } }
  | { type: 'turn.completed'; session_id: string; data: { result: TurnResult } }
  | { type: 'turn.error'; session_id: string; data: { turn_id: string; error: string; stderr?: string[] } }
  | { type: 'message'; session_id: string; data: { message: NormMessage; replaces_partial?: string | null; replaces_local?: string | null } }
  | { type: 'stream.block_start'; session_id: string; data: { partial: PartialBlock } }
  | { type: 'stream.delta'; session_id: string; data: { key: string; text: string } }
  | { type: 'permission.requested'; session_id: string; data: { permission: PermissionReq } }
  | { type: 'permission.resolved'; session_id: string; data: { permission_id: string; decision: string } }
  | { type: 'system.init'; session_id: string; data: { init: InitInfo } }
  | { type: 'notice'; session_id: string; data: { level: 'info' | 'warning' | 'error'; text: string; code?: string } };
