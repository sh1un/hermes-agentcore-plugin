# Compatibility Audit

Status: Partial，native OAuth 尚未接通

## Evidence

檢查 fork commit `6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef`

- Directory Plugin 使用 `plugin.yaml` 與 `__init__.py` 的 `register(ctx)`
- `register_cli_command` 接受 setup 與 handler，初版使用此介面提供 doctor
- `register_command` 的 documented handler 為 raw_args，不能假設有可信 Slack identity
- `pre_gateway_dispatch` 在 `_is_user_authorized_for_source` 前執行，不能直接當成已授權入口
- Fork 查詢 `*2026.8.31*` tag 沒有結果，image 與 source 對應仍未確認

[Plugin API source](https://github.com/sh1un/hermes-agent/blob/6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef/hermes_cli/plugins.py)

[Gateway dispatch source](https://github.com/sh1un/hermes-agent/blob/6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef/gateway/run_inbound.py)

## Implementation Boundary

目前實作 identity syntax、SQLite metadata lifecycle、generation、local disconnect gate 與 diagnostic CLI

`complete_verified` 僅供未來可信 callback service 使用，本身不驗證 OAuth，不能直接暴露成工具

初版 Store 限單 process、單 owner，dispatch 持有 lock 至 operation 完成，Disconnect 會等待已放行的 operation，operation 必須有 timeout

尚未實作 Slack App Home、AWS token exchange、callback HTTP endpoint、provider revoke 或 MCP transport，因此不能部署後直接 Connect

下一步先確認 image source provenance，以及 post-authorization identity 與 native interaction extension，再接通 provider backend
