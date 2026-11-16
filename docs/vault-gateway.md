# Vault Gateway mode

Use `mode=vault_gateway` when your platform stores Cognito tokens in AgentCore
Identity and handles session binding separately. Hermes retrieves an access token
from Identity and uses it to call Gateway. Refresh tokens stay in Token Vault.

## Setup

Your platform operator supplies a workload identity, Cognito credential provider,
Gateway and consent portal URLs, a binding API, and an IAM role your runtime can
assume. The binding service must verify the browser user's identity independently
before calling `CompleteResourceTokenAuth`.

1. Allow your runtime role to assume the platform's agent role. For ECS, use the
   Task Role rather than the execution role. Do not grant the agent binding permissions.
2. Install this plugin and `requirements.txt` in your existing agent image.
   Commit-SHA runtime images are published for `linux/amd64` and `linux/arm64`.
   You can also copy `/opt/hermes/plugins/agentcore` from a digest-pinned image
   into your own Hermes image, install its requirements, and retain your entrypoint.
3. Copy `examples/vault-gateway.toml`, replace the example values, and mount it
   read-only in the task. Keep connection metadata in a task-local directory;
   multiple tasks must not share the same SQLite file.
4. Merge this configuration with your existing Hermes settings:

   ```yaml
   plugins:
     enabled: [agentcore]
     entries:
       agentcore:
         settings:
           config_file: /etc/hermes/agentcore.toml
   platform_toolsets:
     slack: [agentcore]
   ```

Enable the Slack app's Home tab, interactivity, and `app_home_opened` events.
This mode needs no callback listener or inbound port on the agent.

## Sign in and use tools

Open app Home and select **Sign in**. Complete Cognito login and the platform's
browser identity check, then return to Home and select **Refresh sign-in**.
Hermes checks the pending session and enables tool calls when Identity returns
an access token. Authorize Jira separately in the consent portal using the same
Cognito account.

Each tool call retrieves a token from Identity and opens its own Gateway MCP
connection. Keep scopes and custom parameters consistent between calls.
Authorization links stay in native Slack UI; model tools receive no OAuth data.

After a task restart, connect again to reuse the existing Vault grant. Disconnect
blocks local tool calls but does not revoke Cognito or Jira authorizations.

## Binding API

The plugin sends a SigV4-signed `POST` to `binding_api_url` with `user_id`,
`session_uri`, and `state` as JSON strings. The service must return HTTP 201 only
after storing the pending request. `return_url` must use the same API Gateway host.
The service owns browser authentication, expiry checks, and replay prevention.

## Trust boundary

`GetWorkloadAccessTokenForUserId` trusts the agent to supply the correct user ID
from authenticated Slack context. Independent binding protects new authorization
sessions; it cannot stop a compromised agent from retrieving existing tokens for
users it is already allowed to represent. This remains a trusted-agent PoC.

Verify login, refresh, revocation, user isolation, and Gateway/Jira calls in your
own deployment. Unit tests cover local behavior and mocked APIs.
