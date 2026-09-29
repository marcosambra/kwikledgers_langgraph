from __future__ import annotations

import asyncio
import contextvars
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


class FastMCPClientError(RuntimeError):
    """A transport or response failure without request or credential material."""


class CapabilityClient(Protocol):
    def call(self, capability_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one atomic capability and return its normalized data."""


@dataclass(frozen=True)
class FastMCPClientConfig:
    transport: str = "stdio"
    command: str = sys.executable
    args: tuple[str, ...] = ("-m", "kwikledgers_fastmcp.server")
    url: str | None = None
    timeout_seconds: float = 30.0
    env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.transport not in {"stdio", "http"}:
            raise ValueError("FastMCP transport must be stdio or http.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("FastMCP timeout must be finite and positive.")
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("FastMCP stdio command is required.")
        if self.transport == "http":
            parsed = urlsplit(self.url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("FastMCP HTTP URL is required.")
            if parsed.username or parsed.password:
                raise ValueError("Credentials must not be embedded in the FastMCP URL.")

    @classmethod
    def from_environment(cls) -> FastMCPClientConfig:
        args = json.loads(os.environ.get("KWIKLEDGERS_FASTMCP_ARGS", '["-m", "kwikledgers_fastmcp.server"]'))
        if not isinstance(args, list) or not all(isinstance(argument, str) for argument in args):
            raise ValueError("KWIKLEDGERS_FASTMCP_ARGS must be a JSON array of strings.")
        url = os.environ.get("KWIKLEDGERS_FASTMCP_URL")
        return cls(
            transport=os.environ.get("KWIKLEDGERS_FASTMCP_TRANSPORT", "http" if url else "stdio"),
            command=os.environ.get("KWIKLEDGERS_FASTMCP_COMMAND", sys.executable),
            args=tuple(args),
            url=url,
            timeout_seconds=float(os.environ.get("KWIKLEDGERS_FASTMCP_TIMEOUT_SECONDS", "30")),
        )


class FastMCPClient:
    def __init__(self, config: FastMCPClientConfig | None = None) -> None:
        self.config = config if config is not None else FastMCPClientConfig.from_environment()

    def call(self, capability_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return asyncio.run(self.acall(capability_name, arguments))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        context = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(context.run, run).result()

    async def acall(self, capability_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                self._request(capability_name, dict(arguments)), self.config.timeout_seconds,
            )
            return self._normalize(result, capability_name)
        except FastMCPClientError:
            raise
        except asyncio.TimeoutError:
            raise FastMCPClientError("FastMCP request timed out; execution outcome may be unknown. Do not retry automatically.") from None
        except Exception:
            raise FastMCPClientError("FastMCP request failed; execution outcome may be unknown. Do not retry automatically.") from None

    async def _request(self, capability_name: str, arguments: dict[str, Any]) -> Any:
        if self.config.transport == "stdio":
            parameters = StdioServerParameters(
                command=self.config.command, args=list(self.config.args),
                env=dict(os.environ) if self.config.env is None else dict(self.config.env),
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                return await self._call_session(read_stream, write_stream, capability_name, arguments)
        async with streamablehttp_client(
            self.config.url or "", timeout=timedelta(seconds=self.config.timeout_seconds),
        ) as (read_stream, write_stream, _get_session_id):
            return await self._call_session(read_stream, write_stream, capability_name, arguments)

    async def _call_session(
        self, read_stream: Any, write_stream: Any, capability_name: str, arguments: dict[str, Any],
    ) -> Any:
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
        ) as session:
            await session.initialize()
            return await session.call_tool(capability_name, arguments={"arguments": arguments})

    @staticmethod
    def _normalize(result: Any, capability_name: str) -> dict[str, Any]:
        if result.isError:
            raise FastMCPClientError("FastMCP capability returned an error.")
        payload = result.structuredContent
        if payload is None:
            text = "".join(block.text for block in result.content if getattr(block, "type", None) == "text")
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                raise FastMCPClientError("FastMCP response is not a JSON object.") from None
        if not isinstance(payload, dict):
            raise FastMCPClientError("FastMCP response is not a JSON object.")
        if "capability" in payload:
            if payload["capability"] != capability_name or not isinstance(payload.get("data"), dict):
                raise FastMCPClientError("FastMCP capability envelope is invalid.")
            return dict(payload["data"])
        return dict(payload)