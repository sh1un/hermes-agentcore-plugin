# Installation and Validation

This single-instance Linux/macOS PoC implements native Slack Connect, confirmation,
Status, local Disconnect and two read-only Jira operations. Provider-side revocation,
GitHub, Confluence and AgentCore Gateway/Policy are not implemented.

## Requirements

- Python 3.11 or newer
- Hermes `register_platform_handler`, `register_tool`, `get_config`
- Hermes task-local scope, user and platform ContextVars
- Source reference: `6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef`
- Existing image `v2026.8.31` has not been mapped to this source
- One Slack adapter and one process owner per metadata DB
- A test Slack App with Socket Mode, Home Tab and Interactivity enabled
- Subscribe to `app_home_opened` alongside existing message events

All members of configured workspaces can manage their own connections. The Plugin
owns this App's Home page. Use a test App if another plugin publishes a Home page.

## Install

Use an isolated Hermes home and its Python executable first

```bash
python -m pip install -r /path/to/hermes-agentcore-plugin/requirements.txt
python -m pip check
```

Copy this repository to the test Hermes home's `plugins/agentcore` directory.
It must contain `plugin.yaml`, `__init__.py` and `agentcore_core/`. The Python wheel
alone does not register the directory Plugin. Preserve all other Hermes settings.

```yaml
plugins:
  entries:
    agentcore:
      enabled: true
      settings:
        config_file: /path/to/agentcore.toml
```

Copy `examples/agentcore.toml` and replace example values. The metadata directory
must be writable only by the trusted runtime. Settings contain no secret values.
Enable the `agentcore` toolset using the target Hermes version's normal toolset
configuration if it is not enabled automatically.

## AWS and callback

Provision a workload identity and an OAuth provider compatible with Rovo MCP.
Do not assume a generic Atlassian API OAuth app works with Rovo. Register the
application return URL on the workload identity and the distinct AgentCore provider
callback on the OAuth client. The SDK uses the default credential chain, preferably
an IAM role, without a hardcoded profile or access key.

The runtime role requires applicable actions scoped to the workload/provider
resources as supported by AWS

- `bedrock-agentcore:GetWorkloadAccessTokenForUserId`
- `bedrock-agentcore:GetResourceOauth2Token`
- `bedrock-agentcore:CompleteResourceTokenAuth`

Callback listens on `127.0.0.1:8849`. A TLS reverse proxy in the same network
namespace must forward only `/oauth/agentcore/callback`, preserving `session_id`
and `state`. Disable query logging and apply connection/request limits. This small
embedded listener is for PoC use, not an Internet-facing production HTTP server.

## Acceptance test

1. Run `hermes agentcore doctor`, then the isolated `hermes gateway run`
2. Open the App Home and click Connect, then Authorize
3. Complete provider consent and copy the browser's `HAC-` confirmation code
4. Return to Home, click Enter code and submit in the native modal
5. Home displays Connected. Ask for accessible Atlassian resources or a test issue
6. Repeat as a second user and verify separate account access
7. Disconnect the first user. Their new Plugin requests must fail while the second works

Recognizable authorization artifacts pasted into chat are consumed locally. The
modal is the supported confirmation entry point. No slash command is required.

Disconnect stops Plugin requests, not independent existing Hermes MCP connections.
An already admitted request may finish and the UI waits for it. Provider OAuth grants
are not revoked by this release. Do not use another MCP connection as evidence for
this Plugin's disconnect behavior.

## Verification and limits

```bash
python -m unittest discover -s tests -v
```

Tests use fake providers and Botocore Stubber. Live Slack/AWS setup and original
Hermes behavior parity still require the acceptance test. Direct Rovo calls do not
prove AgentCore Gateway Policy enforcement.

No access token is persisted. Tokens exist briefly in the Plugin process, which
does not isolate them from arbitrary code with the same OS identity. Disable SDK
debug, HTTP tracing and Slack payload logging. No LLM sampling callback or OAuth
model tools are registered.

To roll back, disable the Plugin and restart Hermes. Keep the metadata DB to retain
disconnect decisions. Do not delete shared AWS providers when removing the Plugin.
