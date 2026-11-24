# Vault Gateway mode

Use `mode=vault_gateway` when your platform stores Cognito tokens in AgentCore
Identity and handles session binding separately. Hermes retrieves an access token
from Identity and uses it to call Gateway. Refresh tokens stay in Token Vault.

## Setup

Your platform operator supplies a workload identity, Cognito credential provider,
Gateway and consent portal URLs, and a binding API. The Agent Owner manages
the runtime IAM role and its AgentCore access policy. The binding service must
verify the browser user's identity independently before calling
`CompleteResourceTokenAuth`.

1. Create the access role and IAM policies in the Agent Owner repository. Allow
   the ECS Task Role to assume it. Scope access to the registered workload,
   credential provider and users; deny completing bindings or assuming the binder role.
2. Install this plugin and `requirements.txt` in your existing agent image.
   Commit-SHA runtime images are published for `linux/amd64` and `linux/arm64`.
   You can also copy `/opt/hermes/plugins/agentcore` from a digest-pinned image
   into your own Hermes image, install its requirements, and retain your entrypoint.
3. Copy `examples/vault-gateway.toml`, replace the example values, and mount it
   read-only in the task. Keep connection metadata in a task-local directory;
   multiple tasks must not share the same SQLite file. Set `scopes` to the exact
   scopes supplied by your platform operator; the plugin does not choose a
   platform-specific scope.
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

## Sign in again

To test a fresh platform authorization, select **Sign in again** in app Home
and confirm. Open the new **Open sign-in** link, complete Cognito login and the
platform's Slack identity verification, then select **Refresh sign-in**.

The plugin sends `forceAuthentication=true` to `GetResourceOauth2Token` once,
when starting this flow. Identity clears the stored refresh token for that
user's Cognito grant and starts authorization again. Polling, ordinary sign-in,
and tool calls omit the flag. This replaces only the requester's local connection
and pending sign-in; other users keep their connections.

Gateway tools remain blocked locally until the new binding succeeds. If starting
sign-in fails, retry **Sign in again**. Authorization links stay in Slack UI and
are never returned to model tools.

This does not revoke access tokens already issued, disconnect Jira in the consent
portal, or guarantee a password prompt: browser SSO sessions may still be reused.
See [Identity token storage](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html#automatic-refresh-token-storage)
and the [API parameter](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_GetResourceOauth2Token.html).

## v0.2.0 binding policy

Only the verified Slack identity that initiated a new authorization request may
complete its binding. The platform compares workspace and member IDs, not email.
Google login remains subject to the platform's ordinary account restrictions.

For a request initiated by Alice:

| Google login | Verified browser Slack identity | Result |
| --- | --- | --- |
| Alice | Alice | Allow |
| Bob | Bob | Reject |
| Bob | Alice | Allow |
| Bob | No valid Slack session | Reject |

The platform verifies the Slack OIDC ID token, matches it to the registered
initiator, then calls `CompleteResourceTokenAuth` with that verified user ID.
The OAuth callback URL alone is insufficient. No Google-to-Slack email mapping
or separate Slack **Confirm** click is required. An already signed-in browser
may reuse its Slack session; this flow does not prove a fresh, deliberate click.

**Refresh sign-in** polls Identity for the original pending session. It cannot
complete binding or open the local tool gate while authorization is pending.
A different Slack user cannot refresh Alice's pending connection.

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
