# Gemini CLI 0.68 ACP — 드라이버 구현 메모 (조사 에이전트 결과 요약; 스파이크: scratchpad/gemini_spike/acp_spike.py)

실행: node runtime/gemini-cli/node_modules/@google/gemini-cli/bundle/gemini.js --acp   (env GEMINI_CLI_NO_RELAUNCH=true 로 자식 재실행 막기)
전송: 줄 단위 JSON-RPC 2.0 (UTF-8 bytes 로 읽기, StreamReader limit 64MiB), stderr 계속 비우기(시끄러움). 요청은 동시 처리.
agent→client 요청 id 는 0부터 정수 — "method" 있으면 요청. 종료: stdin 닫기(rc 0), OAuth 대기 중엔 taskkill /T /F.

1. initialize {protocolVersion:1, clientCapabilities:{fs:{readTextFile:false,writeTextFile:false},terminal:false}, clientInfo}
   ← authMethods[oauth-personal, gemini-api-key, vertex-ai, gateway], agentCapabilities{loadSession:true, promptCapabilities{image,audio,embeddedContext}}
   fs 는 광고하지 말 것 (새 파일 쓰기 버그).
2. 로그인 안 됨: session/new → -32000 "Gemini API key is missing…". 로그인 = authenticate {methodId:"oauth-personal"}:
   에이전트가 직접 브라우저를 열고(127.0.0.1 콜백) 최대 5분 요청을 붙잡음. URL 은 안 알려줌. 실패하면 같은 프로세스 재시도 불가 → 새로 띄우기.
   성공 → ~/.gemini/oauth_creds.json, google_accounts.json, settings.json{security.auth.selectedType:"oauth-personal"} → 다음부터 authenticate 불필요.
   selectedType 은 oauth 인데 creds 없으면 initialize 가 브라우저 로그인까지 멈춤 → 시작 전에 oauth_creds.json 확인, 20초 넘으면 '브라우저 로그인 대기'.
   NO_BROWSER=true 쓰지 말 것 (stdout 깨짐). API 키: env GEMINI_API_KEY 또는 authenticate gemini-api-key _meta.api-key.
   로그아웃 = oauth_creds.json + google_accounts.json 지우기.
3. session/new {cwd(절대), mcpServers:[]} (mcpServers 필수) ← {sessionId, modes{availableModes default/autoEdit/yolo/plan, currentModeId}, models{availableModels[{modelId,name}], currentModelId}}
   session/set_mode {sessionId, modeId} (autoEdit/yolo 는 신뢰 안 된 폴더에서 거부 — GEMINI_CLI_TRUST_WORKSPACE=true 또는 ~/.gemini/trustedFolders.json)
   모드 바뀜은 agent_message_chunk "[MODE_UPDATE] <id>" 로 옴 → 걸러낼 것. session/set_model {sessionId, modelId} (아무 글자나 OK).
   session/list·resume·close 없음(-32601).
4. session/prompt {sessionId, prompt:[{type:text,text},{type:image,mimeType,data(base64)},{type:resource_link,uri:"file:///…",name}]}
   ← {stopReason: end_turn|cancelled|max_turn_requests|max_tokens, usage{inputTokens,outputTokens,totalTokens,cachedReadTokens?,thoughtTokens?}}
   '/'로 시작하는 글은 로컬 명령. 오류는 code=HTTP 상태(429 등) 또는 -32603 data.details.
   session/update {sessionId, update}:
     agent_message_chunk{content{type:text,text}} · agent_thought_chunk{content{text}} · usage_update{used,size}
     tool_call{toolCallId,status pending|in_progress,title,kind edit|execute|read|…,locations[{path}],content[{type:diff,path,oldText,newText,_meta.kind}|{type:content,content{type:text,text}}]}
     tool_call_update{toolCallId,status completed|failed,content,…} · available_commands_update
5. session/request_permission (agent→client, id): params{sessionId, options[{optionId:proceed_always,kind:allow_always},{proceed_once,allow_once},{cancel,reject_once}], toolCall{…}}
   응답 {outcome:{outcome:"selected",optionId}} / 취소 뒤에는 {outcome:{outcome:"cancelled"}}.
   proceed_always(편집) → 세션 전체 autoEdit 로 바뀜 → 우리 화면은 '항상 허용'을 이 대화 한정으로만 (또는 안 보여줌). 거부 → tool_call_update failed, 모델 계속.
6. session/cancel {sessionId} (알림, id 없음) → prompt 가 {stopReason:"cancelled"} 로 끝남. 셸 자식 정리됨.
7. session/load {sessionId, cwd(같아야 함), mcpServers:[]} → 응답 전에 기록 전체를 session/update 로 다시 흘림(user_message_chunk 포함, 잡음 많음)
   → 화면 기록은 우리가 따로 저장, load 는 모델 문맥 복원용. 기록 파일: ~/.gemini/tmp/<slug>/chats/session-<시각>-<id 앞8>.jsonl
8. GEMINI_CLI_HOME 으로 앱 전용 홈 가능 (테스트는 이걸로 격리). 셸은 PowerShell. node-pty AttachConsole 오류는 무해.
