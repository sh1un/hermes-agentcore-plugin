# Hermes AgentCore Plugin

Hermes plugin for native Slack account linking and per-user AWS Bedrock AgentCore access

## v0.2.0

Prepared in source: [`mode=vault_gateway`](docs/vault-gateway.md) uses AgentCore
Identity for Cognito tokens and a separate platform service for browser Slack
identity verification. **Refresh sign-in** reads the result; it cannot complete
binding. See the [release notes](docs/releases/v0.2.0.md).

## Earlier MVP

The first MVP release is `v0.1.0`. Pin plugin source to that Git tag or use
`ghcr.io/sh1un/hermes-agentcore-plugin:v0.1.0` for the bundled Hermes runtime.
Prefer the published digest for immutable deployments.
See [release notes and limitations](docs/releases/v0.1.0.md).

## Current status

Existing `mode=portal`: native Cognito login with PKCE, Slack account confirmation,
portal entry, and per-user read-only Jira dispatch through AgentCore Gateway

No OAuth artifacts are exposed by authorization tooling to
the LLM. In portal mode, Cognito access tokens live only in memory until expiry or
local sign-out; restart requires sign-in again. Vault Gateway mode retrieves them
from Identity instead. Local sign-out does not revoke provider grants

Live integration behavior, Gateway Policy outcomes and compatibility with each
host deployment require separate acceptance testing. Unit tests are not production acceptance

[Consent portal design](docs/consent-portal.md) and [portal setup](docs/portal-installation.md)

Portal tool dispatch now uses immutable per-request login snapshots, bounded
concurrency and stale-result rejection after sign-out, expiry or account change.
See [requester dispatch design and native MCP limitations](docs/requester-dispatch.md).
This does not cancel already admitted upstream requests or add generic MCP tools.

The original direct Rovo PoC remains available for existing configurations

[Installation and acceptance test](docs/installation.md)

See the [compatibility audit](docs/compatibility-audit.md) for host API requirements
and integration gaps

## Development

```bash
uv run --no-project --python 3.14 --with-requirements tests/requirements.txt -m unittest discover -s tests -v
```

Directory plugin layout uses `plugin.yaml` and `__init__.py` with `register(ctx)`
The diagnostic command is `hermes agentcore doctor`. Configured installations also
register `agentcore_jira_read`. OAuth actions run only through native Slack UI

The host must authenticate Slack events before constructing a `SlackIdentity`
ID syntax validation is not authentication

Legacy direct mode uses a dedicated SQLite metadata store. Portal mode does not
use that store and never imports legacy connection state automatically

## Design

[Technical design and acceptance criteria](docs/technical-design.md)

Commit messages are English and follow Conventional Commits 1.0.0-beta.4

## Vault Gateway mode

With [`mode=vault_gateway`](docs/vault-gateway.md), Identity stores Cognito tokens
and your platform handles browser session binding. Hermes runs in your existing
agent deployment and calls Gateway without a local callback listener.
Start with [`examples/vault-gateway.toml`](examples/vault-gateway.toml).

## Host dependency compatibility

Image builds validate plugin pins against the pinned Hermes project, including its optional dependencies, before installing packages. They use Hermes' lock-aware installer and preserve its declared overrides. A standalone dependency check is insufficient: it can pass while Hermes later rejects the plugin's project requirements.

After validation, the image retains the plugin's source project metadata as `pyproject.build.toml`. The runtime payload is a prebuilt directory plugin, so Hermes does not treat it as a new source workspace or reinstall dependencies at startup. Builds reject a native doctor result that reports an out-of-sync environment.
