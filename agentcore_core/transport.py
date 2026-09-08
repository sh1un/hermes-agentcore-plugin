"""One MCP connection per invocation. No shared auth headers or model callbacks."""
import asyncio
import logging
import re


LOG = logging.getLogger("agentcore.gateway")


def diagnostic(stage, outcome, *, status=None, code=None, request_id=None, protocol=None):
    """Operator-only allowlisted metadata. Never log exceptions or response bodies."""
    fields = {"stage": stage if stage in {"connect", "initialize", "call_tool", "filter"} else "other",
              "outcome": outcome if outcome in {"ok", "failed", "rejected", "timeout"} else "other"}
    if type(status) is int and 100 <= status <= 599:
        fields["http_status"] = status
    if type(code) is int and -32768 <= code <= 32767:
        fields["rpc_code"] = code
    if isinstance(request_id, str) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", request_id):
        fields["request_id"] = request_id
    if protocol in ("2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"):
        fields["protocol"] = protocol
    LOG.warning("gateway_diagnostic %s", fields)


async def _call(provider, token, tool, arguments):
    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    stage = "connect"

    async def response_metadata(response):
        diagnostic(stage, "ok" if response.status_code < 400 else "failed",
                   status=response.status_code, request_id=response.headers.get("x-amzn-requestid"))

    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                     timeout=20, follow_redirects=False,
                                     event_hooks={"response": [response_metadata]}) as http:
            async with streamable_http_client(provider.endpoint, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1],
                                         read_timeout_seconds=20) as session:
                    stage = "initialize"
                    initialized = await session.initialize()
                    diagnostic(stage, "ok", protocol=initialized.protocol_version)
                    stage = "call_tool"
                    result = await session.call_tool(tool, arguments)
                    diagnostic(stage, "failed" if result.is_error else "ok")
                    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    except BaseException as error:
        # Task groups can wrap RPC errors. Keep only integer codes, never messages/data.
        pending = [error]
        for _ in range(16):
            if not pending:
                break
            item = pending.pop()
            diagnostic(stage, "timeout" if isinstance(item, TimeoutError) else "failed",
                       code=getattr(item, "code", None))
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions[:16])
        raise


def call_mcp(provider, token, tool, arguments):
    async def bounded():
        async with asyncio.timeout(30):
            return await _call(provider, token, tool, arguments)
    return asyncio.run(bounded())
