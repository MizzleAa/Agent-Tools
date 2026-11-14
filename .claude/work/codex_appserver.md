# Codex app-server 0.167 — 드라이버 구현 메모 (조사 결과 요약; 재사용 클라이언트: scratchpad/codex_spike/codex_rpc.py, 스파이크 spike.py)

!! 로그인 상태: `codex login status` 는 "Logged in" 이지만 refresh token 만료 → 실제 턴은 error codexErrorInfo "unauthorized".
   사전 확인은 account/read {} → {"account":null,"requiresOpenaiAuth":true} 면 로그인 필요. 사용자에게 다시 로그인 안내 (codex login — 화면 버튼).

실행: codex.exe app-server (stdio). 줄 단위 JSON, "jsonrpc" 필드 없음.
 요청 {"id","method","params"} / 응답 {"id","result"|"error"} / 알림 {"method","params"} / 서버 요청 {"method","id","params"} (서버 id 0부터 따로)
 종료: stdin 닫기 → 1초 안에 rc 0. StreamReader limit 크게, bytes→UTF-8. stdout 읽는 루프 안에서 request() 를 await 하지 말 것 (교착).
 CODEX_HOME 으로 홈 옮김 (테스트 격리).

handshake: initialize {clientInfo{name,title,version}, capabilities{experimentalApi:false}} ← {userAgent, codexHome,…}; 그다음 알림 {"method":"initialized"}
model/list {} ← data[{id,isDefault,supportedReasoningEfforts[{reasoningEffort}],defaultReasoningEffort,inputModalities}] — effort 검증은 이걸로 (서버는 검증 안 함)
account/read {} ← {account|null, requiresOpenaiAuth}

thread/start {cwd, model?, approvalPolicy:"untrusted"|"on-request", sandbox:"read-only"|"workspace-write", config{model_reasoning_effort, model_reasoning_summary}, developerInstructions? (스키마 확인 필요), baseInstructions?}
 ← {thread{id(UUIDv7),path,cwd,status,turns:[]}, model, reasoningEffort,…} + 알림 thread/started
 approvalsReviewer 는 "user" 유지 (auto_review 금지).
turn/start {threadId, input:[{type:"text",text,text_elements:[]},{type:"localImage",path}], effort?, model?, summary?} ← {turn{id,status:"inProgress"}} (턴 설정은 다음 턴에도 이어짐)
알림:
 turn/started · thread/status/changed{status{type active|idle|systemError, activeFlags["waitingOnApproval"]}}
 item/started|item/completed {item{type: userMessage|reasoning|commandExecution|fileChange|agentMessage|contextCompaction, id,…}}
 item/agentMessage/delta {itemId,delta} · item/reasoning/summaryTextDelta {itemId,delta,summaryIndex}
 commandExecution item {command,cwd,status inProgress|completed|declined|failed,aggregatedOutput,exitCode,durationMs} + item/commandExecution/outputDelta {itemId,delta}
 fileChange item {changes[{path,kind{type add|delete|update,move_path},diff}], status inProgress|completed|declined} · turn/diff/updated {diff}
 thread/tokenUsage/updated {tokenUsage{total{inputTokens,cachedInputTokens,outputTokens,reasoningOutputTokens,totalTokens},last,modelContextWindow}}
 turn/completed {turn{id,status completed|interrupted|failed,error,durationMs}}
 error {error{message,codexErrorInfo,…},willRetry,threadId,turnId} → 이어서 turn/completed failed
 serverRequest/resolved {threadId,requestId} (승인 창 닫기)
승인 (서버→클라이언트 요청):
 item/commandExecution/requestApproval {threadId,turnId,itemId,command,cwd,commandActions,availableDecisions} → {"decision":"accept"|"decline"|"cancel"|"acceptForSession"}
 item/fileChange/requestApproval {threadId,turnId,itemId,reason,grantRoot} → 같은 decision (diff 는 같은 itemId 의 item/started fileChange 에서)
 그 밖(item/tool/requestUserInput, item/permissions/requestApproval, mcpServer/elicitation/request, execCommandApproval/applyPatchApproval)은 decline/거절로 답할 것.
중단: turn/interrupt {threadId,turnId} ← {} 후 turn/completed interrupted (진행 중 agentMessage 는 item/completed 없음 → 클라이언트가 마무리). 끝난 턴에 보내면 응답 없음 → 활성 턴에만, 타임아웃.
재개: thread/resume {threadId, approvalPolicy, sandbox, excludeTurns:true} 후 thread/turns/list {threadId,limit,sortDirection:"desc",itemsView:"full",cursor} ← {data[Turn{id,status,items[]}],nextCursor}
 (thread/read {threadId,includeTurns:true} 는 로드 없이 읽기). thread/list {limit,cwd}.
 기록에서 빠지는 것: 중단된 턴의 부분 답, 거절·대기 중 명령. 거절된 fileChange 는 남음.
저장: %USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-…-<threadId>.jsonl
Windows: 샌드박스 강제 안 됨 (read-only 여도 승인한 쓰기 명령은 씀) → 승인 창이 유일한 안전장치 → approvalPolicy "untrusted" (모든 비자명 명령·패치에 물음). 명령은 powershell.exe -Command.
codex exec --json 은 승인 불가 → app-server 만.
