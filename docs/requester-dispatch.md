# Requester-scoped Dispatch

## Decision

借用 OpenClaw per-requester 的身份分流概念，不照搬 model-visible Connect URL 或本地 OAuth credential store
本次修改只針對 portal mode，direct mode 保留既有行為
不修改 Hermes 原始碼，不使用 monkey patch，不引入 OpenAB

## Evidence

檢查 Hermes fork commit 6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef

- tools/mcp_tool.py 的 _servers 以 server name 保存 MCPServerTask
- tools/mcp_tool_transport.py 在連線建立時讀取 config headers 與 OAuth auth
- tools/mcp_oauth_provider.py 建立 HermesTokenStorage(server_name)
- hermes_cli/plugins.py 提供 register_tool 與 register_platform_handler，未找到可直接使用的 per-request MCP credential resolver
- task-local identity 仍依賴 gateway/session_context.py 的 private _VAR_MAP，不是穩定 public API

[Hermes source](https://github.com/sh1un/hermes-agent/tree/6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef)

[OpenClaw per-requester OAuth](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md#oauth-workflow)

上述證據只涵蓋檢查版本，不能推論所有未來版本都沒有 resolver

## Implementation

工具參數只有業務參數，身份由 host task-local context 取得
每次 dispatch 在 lock 內取得該 Slack subject 當下的 Login object，作為本次授權快照
HTTP 使用 request-local client，不更新全域 headers，也不共用 MCP session
network I/O 不持有全域登入狀態 lock，其他使用者可以呼叫或登出
同一 requester 最多兩個 in-flight requests，整個 runtime 最多 32 個，超過即回傳 busy，不排隊保留 token
回傳模型前再次檢查登入 object 與 expiry，signout、close、帳號重新綁定後的舊結果直接丟棄
成功結果仍經既有 auth metadata 過濾，例外與 OAuth elicitation 不交給模型

## Limits

Sign out 是本地停止後續授權，不是 provider revoke
已取得授權快照的 in-flight read 可能已送出，也可能在登出後才抵達上游，不能宣稱撤回已送出的請求
舊結果會在回傳邊界被丟棄，已交付 Hermes 的結果無法收回
本次仍只有兩個唯讀 Jira aliases，不開放泛用 write tools
不把 per-requester OAuth 等同整個 Hermes 的記憶、檔案、對話多租戶隔離
Cognito callback exchange 仍使用原有序列化邏輯，本次不改登入 state machine
真實 Slack／AWS 雙人驗收及 image provenance 仍待完成

## Upstream Follow-up

若要沿用 Hermes 原生 discovery 與 tool schema，需要穩定的 requester connection resolver
resolver 必須接收 host 驗證的 requester，不接受 tool args 或 process env 指定身份
連線與 discovery cache 必須按 server、requester、login generation 分開，缺失身份時 fail closed
resolver 不應把 credential 注入共享 config，auth required event 只交給 native UI
在此介面存在且完成驗證之前，保留薄 MCP SDK transport，不以改全域 headers 假裝完成原生整合
