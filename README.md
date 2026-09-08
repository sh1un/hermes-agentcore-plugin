# Hermes AgentCore Plugin

Experimental Hermes plugin for per-user AWS AgentCore integration without OpenAB

## Current status

Implemented: a diagnostic CLI entry point and a dependency-free connection metadata core
with subject isolation, generation checks, restart recovery and a local disconnect gate

Not yet implemented: native Slack connection UI, AWS OAuth exchange, callback server,
MCP transport and remote revocation. This is not ready for live authorization

The reference image tag has not yet been mapped to source. See the
[compatibility audit](docs/compatibility-audit.md) for verified APIs and gaps

## Development

```bash
python3 -m unittest discover -s tests -v
```

Directory plugin layout uses `plugin.yaml` and `__init__.py` with `register(ctx)`
The only registered host command is `hermes agentcore doctor`
No model tools or OAuth actions are registered at this stage

The host must authenticate Slack events before constructing a `SlackIdentity`
ID syntax validation is not authentication

The SQLite store requires a dedicated trusted directory and one process owner
It stores connection state only, never provider tokens. Local disconnect does
not imply provider-side revocation

## Design

[Technical design and acceptance criteria](docs/technical-design.md)

Commit messages are English and follow Conventional Commits 1.0.0-beta.4
