# Compatibility Audit

Status: 已完成 PoC 接線，live Slack/AWS 與既有 image 相容性尚未驗證

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

已實作 native Slack middleware、Boto3 Identity、callback、confirmation 與唯讀 MCP dispatch

額外確認 `register_platform_handler` 可取得 Slack AsyncApp，Plugin 使用 middleware 在 conversation listener 前消耗 native events

工具身份直接讀取 `gateway/session_context.py` 的 task-local ContextVars，避開 `get_session_env` 的 process environment fallback，`_VAR_MAP` 是需要追蹤的 private API dependency

`complete_verified` 僅供未來可信 callback service 使用，本身不驗證 OAuth，不能直接暴露成工具

Legacy direct mode 的 Store 限單 process、單 owner，dispatch 持有 lock 至 operation 完成，Disconnect 會等待已放行的 operation
Portal mode 的 tool dispatch 已改為 request-local 登入快照，network I/O 不持有全域 lock，登出後拒絕舊结果
詳見 [Requester dispatch](requester-dispatch.md)，本次不改 Cognito callback exchange 的序列化行為

Provider revoke 與 Gateway/Policy 尚未實作，local Disconnect 不宣稱撤銷第三方 grant

安裝前仍須確認 image source provenance，並使用隔離的 Slack App 驗證兩個使用者的實際授權
