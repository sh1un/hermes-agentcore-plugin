import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agentcore_core.transport import _call, diagnostic


class TransportTests(unittest.TestCase):
    def test_diagnostics_do_not_serialize_arbitrary_values(self):
        with self.assertLogs("agentcore.gateway") as logs:
            diagnostic("private-token", "https://private.example?code=secret", status="secret",
                       code="secret", request_id="secret", protocol="secret")
            diagnostic("call_tool", "failed", status=403, code=-32603,
                       request_id="12345678-1234-1234-1234-123456789abc")
        text = str(logs.output)
        for secret in ("private-token", "private.example", "secret"):
            self.assertNotIn(secret, text)
        self.assertIn("403", text)
        self.assertIn("-32603", text)

    def test_initialize_and_call_are_distinguishable_without_credentials(self):
        provider = Mock(endpoint="https://gateway.example/mcp")
        session = AsyncMock()
        session.initialize.return_value = Mock(protocolVersion="2025-11-25")
        result = Mock(isError=False)
        result.model_dump.return_value = {"content": [], "isError": False}
        session.call_tool.return_value = result

        @asynccontextmanager
        async def streams(*args, **kwargs):
            yield (None, None, None)

        with patch("httpx2.AsyncClient") as client, patch("mcp.ClientSession") as factory, \
                patch("mcp.client.streamable_http.streamable_http_client", streams):
            client.return_value.__aenter__.return_value = Mock()
            factory.return_value.__aenter__.return_value = session
            with self.assertLogs("agentcore.gateway") as logs:
                value = asyncio.run(_call(provider, "private-token", "tool", {"secret": "private-argument"}))
            self.assertFalse(value["isError"])
            self.assertIn("initialize", str(logs.output))
            self.assertIn("call_tool", str(logs.output))
            self.assertIn("2025-11-25", str(logs.output))
            self.assertNotIn("private", str(logs.output))
            session.initialize.side_effect = RuntimeError("private-token https://secret?code=private")
            session.call_tool.reset_mock()
            with self.assertLogs("agentcore.gateway") as logs, self.assertRaises(RuntimeError):
                asyncio.run(_call(provider, "private-token", "tool", {}))
            self.assertNotIn("private", str(logs.output))
            session.call_tool.assert_not_called()
