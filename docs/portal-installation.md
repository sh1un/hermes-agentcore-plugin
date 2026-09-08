# Portal Setup

## Scope

這是獨立 Hermes directory Plugin
不要直接修改正式環境，先使用獨立 Hermes home 與測試 Slack App
Hermes host API 相容限制請先讀 [compatibility audit](compatibility-audit.md)

## AWS Prerequisites

- 已有 ACTIVE Consent portal 與 JWT Gateway
- Cognito pool 必須與 portal 主 IdP 及 Gateway issuer 相同
- Plugin 使用 public app client，不含 client secret，允許 Authorization Code flow
- client scope 包含 openid、email、profile、agentcore/gateway.invoke
- Gateway allowedClients 包含 Plugin 使用的 client ID
- 該 client 允許 HTTPS Plugin callback，例如 https://hermes-login.example.com/oauth/cognito/callback
- Cognito domain 是 managed login domain，不是 user pool issuer URL
- Jira target 使用 AUTHORIZATION_CODE，defaultReturnUrl 維持 portal /connect/callback

目前既有 MCP client 只有 localhost / Claude callback，不能直接用於已部署的 Hermes
可以由管理員在符合安全要求的 client 加入 callback，或建立專用 public client
本次程式不會替你更改這些 AWS 資源

## Install

使用執行 Hermes 的同一個 Python 環境

```bash
python -m pip install -r /path/to/hermes-agentcore-plugin/requirements.txt
python -m pip check
```

將 repo 放入測試 Hermes home 的 plugins/agentcore
複製 examples/portal.toml 到自己的設定位置，填入真實值
tools 裡的名稱必須從該 Gateway tools/list 確認，範例前綴不是自動發現的保證
只映射兩個對應的唯讀工具，不要把 write tool 映射成 read 名稱

合併進測試 Hermes config.yaml，不覆蓋其他設定

```yaml
plugins:
  entries:
    agentcore:
      enabled: true
      settings:
        config_file: /absolute/path/to/portal.toml
```

在 Hermes 啟用 agentcore toolset，沿用正常啟動方式

```bash
hermes agentcore doctor
hermes gateway run
```

doctor 是載入提示，不是 AWS 或 callback 健康檢查
Portal 模式不呼叫 boto3，不需要把 AWS IAM credentials 傳給模型或 Plugin

## Slack and Callback

- 測試 App 開啟 Socket Mode、Home Tab、Interactivity
- 訂閱 app_home_opened，保留既有訊息訂閱
- Plugin 會擁有 App Home，避免與其他 Home publisher 共用
- 設定 workspace_ids 的成員可管理自己的登入，不代表取得其他人的工具權限
- HTTPS reverse proxy 轉發 /oauth/cognito/callback 到同 network namespace 的 127.0.0.1:8849
- 保留 code 與 state query，不記錄 query、body、Authorization header
- 設定外部 rate limit、connection limit，禁止 SDK debug／Slack payload tracing
- 不修改 portal 的 /callback、/connect/callback

## User Experience

1. Slack 打開 App Home，點 Sign in，再點 Open sign-in
2. 完成 Cognito / Google 登入
3. 回 Slack Home 點 Refresh，確認顯示的帳號是自己，點 Confirm this account
4. 點 Manage Connections，使用同一 Cognito 帳號登入 portal 並 Connect Jira
5. 回 Slack 要求使用 agentcore_jira_read 查詢 accessible resources 或測試 issue

若已先在 portal Connect，可以跳過第 4 步的再次授權，但仍需完成 Slack 身份登入
模型工具參數只有 tool 與 arguments，不接受 subject、token、URL 或 connection override

```json
{"tool":"getAccessibleAtlassianResources","arguments":{}}
```

```json
{"tool":"getJiraIssue","arguments":{"cloudId":"your-cloud-id","issueIdOrKey":"TEST-1"}}
```

Signed in 只表示 Plugin 持有該使用者有效的 Cognito 登入，不宣稱 portal 已 Connect
不同 Cognito 帳號的 portal 授權不會自動合併

## Sign-out and Expiry

Sign out here 清除 Plugin 記憶體中的登入及 pending login
它不是 Cognito global logout，也不是 Jira OAuth revoke，不會影響其他 MCP client
已送出的呼叫可能完成，Sign out 會等待它結束，再阻擋後续呼叫
access token 到期前 30 秒停止使用，不保存 refresh token，重新登入才能繼續
重啟也需要登入，這是本版不落地保存憑證的明確取捨

## Validation

先執行測試，再以兩個真實測試帳號驗收

```bash
python -m unittest discover -s tests -v
```

驗收細節在 [Consent portal design](consent-portal.md)
未驗證 portal 身份沿用、Gateway tool 名稱、實際 Jira 存取與 Policy 前，不算部署完成
