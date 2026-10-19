import unittest
from unittest.mock import AsyncMock

from kwikledgers_fastmcp.shared.operational_context import METADATA_KEY, OperationalContext
from kwikledgers_langgraph.fastmcp_client import FastMCPClient


class ClientMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_timeout_is_bounded_and_does_not_send_operations(self):
        import asyncio
        from contextlib import asynccontextmanager
        from unittest.mock import patch
        from kwikledgers_langgraph.fastmcp_client import FastMCPClientConfig, FastMCPClientError

        @asynccontextmanager
        async def transport(_parameters):
            yield "read", "write"

        session = AsyncMock()
        session.__aenter__.return_value = session
        async def initialize():
            await asyncio.sleep(10)

        session.initialize.side_effect = initialize
        client = FastMCPClient(FastMCPClientConfig(timeout_seconds=0.01))
        with patch("kwikledgers_langgraph.fastmcp_client.stdio_client", transport), \
             patch("kwikledgers_langgraph.fastmcp_client.ClientSession", return_value=session):
            with self.assertRaisesRegex(FastMCPClientError, "nenhuma capability"):
                async with client.session():
                    self.fail("Sessão não deveria inicializar.")
        session.call_tool.assert_not_called()
        session.__aexit__.assert_awaited_once()

    async def test_runtime_session_initializes_once_and_closes_after_calls(self):
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import patch

        opened = []

        @asynccontextmanager
        async def transport(_parameters):
            opened.append("open")
            try:
                yield "read", "write"
            finally:
                opened.append("closed")

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.call_tool.return_value = SimpleNamespace(
            isError=False, structuredContent={"capability": "read", "data": {"ok": True}},
        )
        with patch("kwikledgers_langgraph.fastmcp_client.stdio_client", transport), \
             patch("kwikledgers_langgraph.fastmcp_client.ClientSession", return_value=session):
            client = FastMCPClient(context=OperationalContext("request-1"))
            async with client.session():
                for _ in range(2):
                    self.assertEqual(await asyncio.to_thread(client.call, "read", {}), {"ok": True})
            self.assertIsNone(client._session)
        self.assertEqual(opened, ["open", "closed"])
        session.initialize.assert_awaited_once()
        self.assertEqual(session.call_tool.await_count, 2)
        spans = [call.kwargs["meta"][METADATA_KEY]["span_id"] for call in session.call_tool.call_args_list]
        self.assertNotEqual(*spans)

    async def test_context_uses_metadata_not_business_arguments(self):
        from unittest.mock import patch

        context = OperationalContext("request-1", run_id="run-1")
        client = FastMCPClient(context=context)
        session = AsyncMock()
        session.__aenter__.return_value = session
        with patch("kwikledgers_langgraph.fastmcp_client.ClientSession", return_value=session):
            await client._call_session("read", "write", "get_active_user", {})
        call = session.call_tool.call_args
        self.assertEqual(call.kwargs["arguments"], {"arguments": {}})
        self.assertEqual(call.kwargs["meta"][METADATA_KEY]["request_id"], "request-1")
        self.assertEqual(call.kwargs["meta"][METADATA_KEY]["run_id"], "run-1")
        self.assertTrue(call.kwargs["meta"][METADATA_KEY]["span_id"])
