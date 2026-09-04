from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from kwikledgers_langgraph.cli import run_daily_summary

DAILY_SUMMARY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": "Origem registrada no estado e no tracking (padrao: 'langgraph-mcp').",
        },
        "notifications_enabled": {
            "type": "boolean",
            "description": "Envia notificacao pelo adapter legado de Windows (padrao: false).",
        },
        "backend": {
            "type": "string",
            "description": "Backend de integracao do runtime. O padrao atual e 'legacy'.",
        },
    },
    "additionalProperties": False,
}

server: Server = Server("kwikledgers-langgraph")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="daily_summary",
            description=(
                "Executa o runtime LangGraph do resumo diario da KwikLedgers: itens "
                "atribuidos, bloqueios, PRs abertas, story points restantes e "
                "sincronizacao de tracking local."
            ),
            inputSchema=DAILY_SUMMARY_INPUT_SCHEMA,
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result_text = await _dispatch(name, arguments or {})
        return [TextContent(type="text", text=result_text)]
    except Exception as error:  # noqa: BLE001 - resultado de erro precisa virar texto p/ o cliente MCP
        return [TextContent(type="text", text=f"Erro: {error}")]


async def _dispatch(name: str, arguments: dict[str, Any]) -> str:
    if name == "daily_summary":
        return _run_daily_summary_tool(arguments)
    raise ValueError(f"Tool desconhecida: {name}")


def _run_daily_summary_tool(arguments: dict[str, Any]) -> str:
    result = run_daily_summary(
        source=str(arguments.get("source") or "langgraph-mcp"),
        notifications_enabled=bool(arguments.get("notifications_enabled", False)),
        backend_name=arguments.get("backend"),
    )
    return json.dumps(result, ensure_ascii=False)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
