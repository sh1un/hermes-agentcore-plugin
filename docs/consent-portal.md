# Consent Portal Integration

## Decision

主要授權入口改為 AWS Consent portal，Hermes 不再自己完成 Jira Connect

Plugin 負責 Slack 使用者登入 Cognito、原生確認身份、以該使用者的 access token 呼叫 Gateway
第三方 token 由 Gateway 與 AgentCore Identity 處理，Plugin 不取回 Jira token
此設計透過 Hermes Plugin 介面整合

## Identity and Flow

1. Slack Home 的 Sign in 由原生程式建立 Cognito Authorization Code + PKCE S256 請求
2. state 綁定已驗證的 Slack workspace/member，nonce 綁定 ID token
3. callback 驗證 JWT 簽章、issuer、expiry、token_use、client、nonce、scope、兩種 token 的 sub 一致
4. callback 不立即開通工具，使用者回到 Slack Home 重新整理，在原生按鈕確認帳號
5. 確認完成才將 Slack 身份綁定到 issuer + sub 與記憶體中的 access token
6. 同事在 Consent portal 以相同 Cognito 帳號 Connect Jira
7. Plugin 使用該 access token 呼叫 Gateway，不使用機器身份冒充同事

Slack identity 是本地索引，Cognito sub 是 Gateway 接受的身份，兩者不是同一個字串
不以 email 自動配對，email 只供原生確認畫面顯示
同事直接先去 portal Connect 也可以，但首次回 Slack 仍需登入連結
Portal 的 browser session 不會自動提供 token 給 Slack bot

## Security Boundary

- OAuth URL、state、nonce、code、token 不註冊成模型工具、不放入工具結果
- 原生按鈕回傳在 Slack middleware 消費，不進對話 listener
- callback 是登入 Cognito 的 callback，不是第三方 consent callback
- access token 只存記憶體，丟棄 refresh token，重啟或 token 到期需要重新登入
- 這不是零駐留憑證設計，Plugin process 被侵入仍可能洩漏有效 token
- pending login 最多 256 筆，每筆 10 分鐘，callback 只允許一次
- Slack 確認按鈕綁定該次 login attempt，舊按鈕與其他使用者不能確認
- Sign out 清除本地登入與待確認身份，不宣稱撤銷 Cognito 或 Jira grant
- Gateway 錯誤與 elicitation 不原樣交給 LLM，只回固定錯誤
- 工具僅開放兩種唯讀操作，Gateway 實際工具名稱由管理員明確配置
- 單一 process，呼叫與登出序列化，已送出的操作不能追回

## Configuration and Migration

新設定使用 mode = "portal"，既有未指定 mode 的設定仍維持 legacy direct 模式
這是部署相容設定，不是允許 LLM 處理授權的開關，兩個模式都不交出授權資料
使用 examples/portal.toml，保留 Hermes 其餘 config、skills、memory
Portal 模式不使用 legacy SQLite、provider token exchange 或確認碼頁面

需要準備 Cognito public app client，啟用 code flow 與 PKCE
Gateway allowedClients 必須包含該 client，scope 必須允許 agentcore/gateway.invoke
另外註冊 Plugin HTTPS /oauth/cognito/callback，必須指向使用者瀏覽器可存取的 Plugin callback endpoint
Portal 繼續使用既有 /callback 與 /connect/callback，不覆蓋它們
入口只監聽 loopback，反向代理需與 Plugin 同 network namespace，停用 query log 並設定限流
不可直接將這個 PoC HTTP server 暴露到 Internet

## Acceptance

- 兩位測試使用者各自登入同一 Cognito pool，在 portal Connect 各自 Jira 帳號
- 以 Plugin 工具查詢，確認 Gateway 實際身份與 Jira 存取不串用
- 尚未登入時不發出 Gateway request
- 使用者 A Sign out 後不能呼叫，B 仍能呼叫
- 錯誤 state、nonce、issuer、client、scope、過期 JWT、replay 均拒絕
- callback 完成但尚未按 Slack 確認時不能呼叫
- OAuth 錯誤與 URL 不進模型結果
- 保留 host 既有行為，真實 Slack / AWS 驗收未完成前不能宣稱整合成功

## Limits

不自動讀取 portal Connections 狀態，Signed in 不等於 Jira Connected
不自動撤銷 portal grant，不自動刷新 token，不配置 AWS 或部署 host
保留唯讀工具範圍，不以 target READY 或 scope 名稱當作實際操作成功證據

## References

- [Cognito PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/using-pkce-in-authorization-code.html)
- [Cognito access token](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-access-token.html)
- [Consent portal target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-configure-consent-portal-target.html)
