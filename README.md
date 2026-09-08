# Hermes AgentCore Plugin

Experimental Hermes plugin for per-user AWS AgentCore integration without OpenAB

## Current status

Consent portal mode: native Cognito login with PKCE, Slack account confirmation,
portal entry, and per-user read-only Jira dispatch through AgentCore Gateway

No OpenAB dependency. No OAuth artifacts are exposed by authorization tooling to
the LLM. Cognito access tokens live only in memory until expiry or local sign-out.
Restart requires sign-in again. Provider grants are not revoked by local sign-out

Live Slack/AWS validation, Gateway Policy outcomes and compatibility with the
existing Suma image remain unverified. Tests are not a production acceptance result

[Consent portal design](docs/consent-portal.md) and [portal setup](docs/portal-installation.md)

The original direct Rovo PoC remains available for existing configurations

[Installation and acceptance test](docs/installation.md)

The reference image tag has not yet been mapped to source. See the
[compatibility audit](docs/compatibility-audit.md) for verified APIs and gaps

## Development

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
