"""Exercise real MCP framing and CLI JSON against a loopback API process."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_cli_and_mcp_share_live_text_backend() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "TACIT_DEMO": "true",
        "TACIT_HOST": "127.0.0.1",
        "TACIT_PORT": str(port),
        "TACIT_API_URL": f"http://127.0.0.1:{port}",
        "TACIT_TOKEN": "demo-learner",
        "TACIT_AGENT_TOKEN": "demo-agent",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from tacit_api.main import main; main()"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=1) as client:
            while True:
                try:
                    if client.get(f"http://127.0.0.1:{port}/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("Demo API did not become ready")
                time.sleep(0.05)
        cli = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tacit_cli.main import main; main()",
                "context",
                "--agent",
                "--repo",
                "scheduler",
                "--snapshot",
                "snapshot-1",
                "--commit",
                "demo-commit-1",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert json.loads(cli.stdout)["status"] == "answered"

        async def exercise() -> None:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-c", "from tacit_mcp.server import main; main()"],
                env=env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 6
                    result = await session.call_tool(
                        "get_context",
                        {
                            "repository_id": "scheduler",
                            "snapshot_id": "snapshot-1",
                            "source_commit": "demo-commit-1",
                        },
                    )
                    assert not result.isError
                    payload = result.structuredContent
                    assert payload is not None
                    # SDK may wrap untyped object returns under `result`.
                    data = payload.get("result", payload)
                    assert data["status"] == "answered"
                    assert data["usage"]["stt_calls"] == data["usage"]["tts_calls"] == 0

        asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
