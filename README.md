# Hermes AgentCore Plugin

Hermes plugin for native Slack account linking and per-user AWS Bedrock AgentCore access

## Versioned MVP

The first MVP release is `v0.1.0`. Pin plugin source to that Git tag or use
`ghcr.io/sh1un/hermes-agentcore-plugin:v0.1.0` for the bundled Hermes runtime.
Prefer the published digest for immutable deployments.
See [release notes and limitations](docs/releases/v0.1.0.md).

## Current status

Consent portal mode: native Cognito login with PKCE, Slack account confirmation,
portal entry, and per-user read-only Jira dispatch through AgentCore Gateway

No OAuth artifacts are exposed by authorization tooling to
the LLM. Cognito access tokens live only in memory until expiry or local sign-out.
Restart requires sign-in again. Provider grants are not revoked by local sign-out

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

The working branch includes experimental [channel-bound service identity](docs/channel-service-identity.md).
This is not included in the `v0.1.0` release and is not yet validated for unattended operation.

```bash
python3 -m unittest discover -s tests -v
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
