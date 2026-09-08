# Gateway diagnostics

`gateway_request_failed` does not prove that a user needs to reconnect
Only a missing local login produces `sign_in_required`

The `agentcore.gateway` operator logger emits `gateway_diagnostic` records for
connect, initialize, call_tool and result filtering
Allowed metadata includes HTTP status, integer RPC code, a UUID-shaped AWS
request ID and a known negotiated MCP version
Request IDs are available only when AWS returns the matching response header

The logger never serializes request arguments, headers, URLs, tokens, user
identity, raw exceptions or response bodies
These records are not returned to the LLM
OAuth callbacks and native account connection actions are unchanged

After a failed Slack read, an operator can inspect only these records on the host

```bash
sudo docker logs --since 5m YOUR_HERMES_CONTAINER 2>&1 |
  grep 'gateway_diagnostic'
```

An initialize failure means tools/call has not run
A call_tool failure is not sufficient to distinguish IAM, downstream availability
or delegated authorization without further evidence
A filter rejection means the result was withheld at the model boundary
Do not enable SDK HTTP debug logging or send raw Gateway DEBUG responses to a model

Gateway protocol configuration and the actually negotiated protocol are separate
checks, neither a READY control-plane status nor a Connected portal status proves
that a delegated Jira call succeeds
