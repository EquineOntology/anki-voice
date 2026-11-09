import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from anki_voice import server


@pytest.mark.anyio
async def test_a_tool_error_tells_claude_why():
    server.review.last_action = None
    with pytest.raises(ToolError, match="Nothing to undo") as failure:
        await server.create_server().call_tool("undo", {})
    # The SDK keeps an unexpected error's message from Claude.
    assert not isinstance(failure.value, UnexpectedToolError)
