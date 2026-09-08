"""One MCP connection per invocation. No shared auth headers or model callbacks."""
import asyncio
from datetime import timedelta


async def _call(provider, token, tool, arguments):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                 timeout=20, follow_redirects=False) as http:
        async with streamable_http_client(provider.endpoint, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1],
                                     read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                return result.model_dump(mode="json", exclude_none=True)


def call_mcp(provider, token, tool, arguments):
    async def bounded():
        async with asyncio.timeout(30):
            return await _call(provider, token, tool, arguments)
    return asyncio.run(bounded())
