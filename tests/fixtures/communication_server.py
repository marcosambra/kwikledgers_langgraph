"""Real MCP transport with Azure replaced by deterministic local fixtures."""

import os
from pathlib import Path

from kwikledgers_fastmcp.server import build_fastmcp_server
from kwikledgers_fastmcp.shared import capability_gateway


async def fixture_azure(name, arguments):
    if name != "get_my_daily_summary":
        raise ValueError("Esta fixture não permite acessar o Azure.")
    return {
        "capability": name, "domain": "azure_devops", "operation": "read",
        "data": {
            "sprint_context": {"name": "Sprint fixture", "path": arguments.get("sprint_path", "fixture")},
            "branch_story_id": arguments.get("story_id"),
            "counts": {"assigned_items": 0, "blocked_items": 0, "open_prs": 0},
            "items": [], "generated_at": "2026-10-16T03:00:00Z",
        },
    }


if __name__ == "__main__":
    with Path(os.environ["COMMUNICATION_SMOKE_MARKER"]).open("a") as marker:
        marker.write(f"{os.getpid()}\n")
    capability_gateway.invoke_azure_capability = fixture_azure
    build_fastmcp_server().run()
