# Channel-bound service identity

Experimental follow-up to v0.1.0, not part of that release

## Decision

Administrators may bind selected Slack workspace/channel pairs to a dedicated
Cognito service identity. Other channels retain personal identity routing.
The model cannot choose an identity, service profile or channel through tool arguments.

This first increment uses existing memory-only OAuth login state. It does not
implement background refresh, persistent login recovery or autonomous scheduled jobs.
Restart and token expiration require an administrator to reconnect the service.

## Configuration

Add this section to an otherwise configured portal-mode plugin TOML file.
These are placeholders, not real deployment identifiers.

```toml
[service_accounts.alert_reader]
workspace_id = "TEXAMPLE"
expected_sub = "replace-with-service-cognito-sub"
admin_members = ["UADMIN"]
allowed_members = ["UREADER"]
channel_ids = ["CALERT"]
```

- Pin `expected_sub` to the service user's immutable Cognito sub in the configured issuer
- Administrators must obtain this sub from a trusted identity-management source
- Do not use email, a model response or the first browser login to establish trust
- Admin permission does not imply permission to invoke the service from a channel
- Empty allowlists, unknown workspaces and duplicate channel routes are rejected
- Up to four services are supported in a single plugin instance
- Configuration changes require a restart

## Native connection flow

1. An allowlisted administrator opens Slack App Home
2. The administrator selects the separate service connection button
3. The browser login must use the designated service account
4. Callback validates Cognito claims and rejects a sub other than `expected_sub`
5. The initiating administrator confirms the service account through the native UI
6. The service login is stored separately from the administrator's personal login
7. The administrator opens Consent Portal using that same service Cognito account
   and grants the intended third-party service account access

The Cognito sub check does not independently prove the identity of the downstream
Atlassian account selected during provider consent. Operators must verify that
grant belongs to the intended service account and has appropriate permissions.
Use separate browser profiles to avoid unintentionally reusing personal sessions.

Another administrator cannot confirm an attempt they did not initiate. Service
sign-out invalidates pending service attempts across administrators and blocks
new channel calls. It does not revoke the external provider grant.

## Execution boundary

The host adapter reads task-local workspace, requester and channel ContextVars.
It never falls back to process environment values. Slack threads use their parent
channel ID, not their thread timestamp. Missing channel context fails closed when
service routing is configured. Cron requests remain rejected.

For configured channel routes, the requester must be explicitly allowlisted.
The runtime uses the service login exclusively, even if a personal login exists.
Unconnected or expired service logins return `service_sign_in_required`, never a
personal login prompt. Unconfigured channels retain personal routing.

Concurrent users of the same service share its two-request limit. The global
limit remains 32 requests. In-flight calls hold an immutable login snapshot and
their results are rejected after service sign-out, replacement or expiration.
Already admitted network requests are not cancelled by sign-out.

The `agentcore.identity` INFO logger records admitted requester, channel, effective
local principal and tool name. It does not record tokens, OAuth URLs or tool data.
These logs are operator-only, not model context. Existing Gateway diagnostics
provide transport outcomes separately.

## Limits and rollout gates

- Only the existing two read-only Jira tools are exposed, not Confluence or writes
- Service-account permissions must restrict which Jira data can be read
- Do not enable against sensitive data until host session/memory/tool isolation is verified
- This feature isolates credential selection, not all Hermes memory or filesystem access
- Use a dedicated test Hermes home and restricted test service account first
- Automatic alert ingestion and bot-origin identity handling require separate host verification
- No machine credential refresh or persisted refresh-token store is introduced
- Live Cognito, Consent Portal, Gateway and Slack acceptance remains required

## Acceptance

Verify a configured channel uses the service account while a DM uses the requester.
Test wrong-account login, unauthorized admin/member actions, missing channel context,
cross-workspace reuse, concurrent requests and sign-out during a call.
Restart must fail closed with service reconnection required, not silently borrow a
personal login. Verify audit logs without exposing tokens to the model.
