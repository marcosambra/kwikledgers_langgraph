import unittest
from unittest.mock import AsyncMock

from kwikledgers_fastmcp.shared.operational_context import METADATA_KEY, OperationalContext
from kwikledgers_langgraph.fastmcp_client import FastMCPClient


class ClientMetadataTests(unittest.IsolatedAsyncioTestCase):
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
