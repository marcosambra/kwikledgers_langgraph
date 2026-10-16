from __future__ import annotations

import asyncio
import contextvars
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, AsyncIterator, Protocol
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from kwikledgers_fastmcp.shared.operational_context import OperationalContext


def _default_stdio_command() -> str:
    local_fastmcp_python = Path(__file__).resolve().parents[2] / "kwikledgers_fastmcp" / ".venv" / "bin" / "python"
    return str(local_fastmcp_python) if local_fastmcp_python.is_file() else sys.executable


class FastMCPClientError(RuntimeError):
    """A transport or response failure without request or credential material."""

    def __init__(self, message: str, *, execution_outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.execution_outcome_unknown = execution_outcome_unknown


class CapabilityClient(Protocol):
    def call(self, capability_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one atomic capability and return its normalized data."""


@dataclass(frozen=True)
class FastMCPClientConfig:
    transport: str = "stdio"
    command: str = _default_stdio_command()
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
            if parsed.hostname != "localhost":
                try:
                    loopback = ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    raise ValueError("Unauthenticated FastMCP HTTP requires a loopback host.")

    @classmethod
    def from_environment(cls) -> FastMCPClientConfig:
        args = json.loads(os.environ.get("KWIKLEDGERS_FASTMCP_ARGS", '["-m", "kwikledgers_fastmcp.server"]'))
        if not isinstance(args, list) or not all(isinstance(argument, str) for argument in args):
            raise ValueError("KWIKLEDGERS_FASTMCP_ARGS must be a JSON array of strings.")
        url = os.environ.get("KWIKLEDGERS_FASTMCP_URL")
        try:
            timeout_seconds = float(os.environ.get("KWIKLEDGERS_FASTMCP_TIMEOUT_SECONDS", "30"))
        except ValueError:
            timeout_seconds = 30.0
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            timeout_seconds = 30.0
        return cls(
            transport=os.environ.get("KWIKLEDGERS_FASTMCP_TRANSPORT", "http" if url else "stdio"),
            command=os.environ.get("KWIKLEDGERS_FASTMCP_COMMAND", _default_stdio_command()),
            args=tuple(args),
            url=url,
            timeout_seconds=min(timeout_seconds, 3600.0),
        )


class FastMCPClient:
    def __init__(
        self, config: FastMCPClientConfig | None = None, *, context: OperationalContext | None = None,
    ) -> None:
        self.config = config if config is not None else FastMCPClientConfig.from_environment()
        self.context = context if context is not None else OperationalContext.from_environment()
        self._session: ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FastMCPClient]:
        """Keep transport and initialization scoped to one runtime execution."""
        if self._session is not None:
            raise FastMCPClientError("A sessão MCP já está aberta.")
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    streams = await stack.enter_async_context(self._streams())
                    session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                    await session.initialize()
            except TimeoutError:
                raise FastMCPClientError("Tempo limite ao inicializar a sessão MCP; nenhuma capability enviada.") from None
            self._session, self._session_loop = session, asyncio.get_running_loop()
            try:
                yield self
            finally:
                self._session, self._session_loop = None, None

    def _timeout_for_capability(self, capability_name: str) -> float:
        if capability_name in {"create_calendar_event", "schedule_reminder", "get_upcoming_deadlines"}:
            variable, default, attempts, margin = "KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS", 90, 1, 15
        elif capability_name == "send_notification":
            variable, default, attempts, margin = "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS", 20, 2, 5
        else:
            return self.config.timeout_seconds
        environment = os.environ if self.config.env is None else self.config.env
        try:
            server_timeout = int(environment.get(variable, str(default)))
        except ValueError:
            server_timeout = default
        if server_timeout <= 0:
            server_timeout = default
        return max(self.config.timeout_seconds, min(server_timeout, 300) * attempts + margin)

    def call(self, capability_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session_loop is not None:
            try:
                caller_loop = asyncio.get_running_loop()
            except RuntimeError:
                caller_loop = None
            if caller_loop is self._session_loop:
                raise FastMCPClientError("Use acall no loop proprietário da sessão MCP.")
            return asyncio.run_coroutine_threadsafe(
                self.acall(capability_name, arguments), self._session_loop,
            ).result()

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
                self._request(capability_name, dict(arguments)), self._timeout_for_capability(capability_name),
            )
            return self._normalize(result, capability_name)
        except FastMCPClientError:
            raise
        except asyncio.TimeoutError:
            raise FastMCPClientError(
                "FastMCP request timed out; execution outcome may be unknown. Do not retry automatically.",
                execution_outcome_unknown=True,
            ) from None
        except Exception:
            raise FastMCPClientError(
                "FastMCP request failed; execution outcome may be unknown. Do not retry automatically.",
                execution_outcome_unknown=True,
            ) from None

    async def _request(self, capability_name: str, arguments: dict[str, Any]) -> Any:
        if self._session is not None:
            return await self._invoke_session(self._session, capability_name, arguments, reused=True)
        async with self._streams(capability_name) as streams:
            return await self._call_session(streams[0], streams[1], capability_name, arguments)

    @asynccontextmanager
    async def _streams(self, capability_name: str = "") -> AsyncIterator[Any]:
        if self.config.transport == "stdio":
            parameters = StdioServerParameters(
                command=self.config.command, args=list(self.config.args),
                env=dict(os.environ) if self.config.env is None else dict(self.config.env),
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                yield read_stream, write_stream
        else:
            seconds = self._timeout_for_capability(capability_name) if capability_name else max(
                self._timeout_for_capability(name) for name in ("create_calendar_event", "send_notification")
            )
            timeout = timedelta(seconds=seconds)
            async with streamablehttp_client(
                self.config.url or "", timeout=timeout, sse_read_timeout=timeout,
            ) as (read_stream, write_stream, _get_session_id):
                yield read_stream, write_stream

    async def _call_session(
        self, read_stream: Any, write_stream: Any, capability_name: str, arguments: dict[str, Any],
    ) -> Any:
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=timedelta(seconds=self._timeout_for_capability(capability_name)),
        ) as session:
            await session.initialize()
            return await self._invoke_session(session, capability_name, arguments)

    async def _invoke_session(
        self, session: ClientSession, capability_name: str, arguments: dict[str, Any], *, reused: bool = False,
    ) -> Any:
        options: dict[str, Any] = {}
        if self.context is not None:
            options["meta"] = self.context.child().metadata()
        if reused:
            options["read_timeout_seconds"] = timedelta(seconds=self._timeout_for_capability(capability_name))
        return await session.call_tool(capability_name, arguments={"arguments": arguments}, **options)

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