# Hermes AgentCore Plugin

Experimental Hermes plugin for per-user AWS AgentCore integration without OpenAB

## Current status

Implemented PoC: native Slack Home Connect, confirmation modal, callback server,
per-user AWS token exchange, read-only Jira MCP dispatch and local Disconnect

Provider-side revocation and AgentCore Gateway/Policy are not implemented.
Live Slack/AWS validation and compatibility with the existing image remain unverified

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

The SQLite store requires a dedicated trusted directory and one process owner
It stores connection state only, never provider tokens. Local disconnect does
not imply provider-side revocation

## Design

[Technical design and acceptance criteria](docs/technical-design.md)

Commit messages are English and follow Conventional Commits 1.0.0-beta.4
