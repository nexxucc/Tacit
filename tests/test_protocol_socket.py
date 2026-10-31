"""Real Uvicorn/WebSocket handshakes without providers or credentials."""

import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from tacit_api.compatibility import ProtocolCompatibility
from tacit_contracts.versions import protocol_headers
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect


@pytest.fixture
def protocol_server():
    application = FastAPI()
    entered = []

    @application.websocket("/echo")
    async def echo(ws: WebSocket):
        entered.append(True)
        await ws.accept()
        await ws.send_text("accepted")
        await ws.close()

    application.add_middleware(ProtocolCompatibility)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(application, log_config=None, log_level=None, lifespan="off")
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("Loopback server failed to start")
            time.sleep(0.01)
        yield f"ws://127.0.0.1:{port}/echo", entered
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def test_real_websocket_mismatch_returns_426_before_endpoint(protocol_server):
    url, entered = protocol_server
    with pytest.raises(InvalidStatus) as failed:
        connect(url, additional_headers={"X-Tacit-API-Version": "999"}, proxy=None)
    response = failed.value.response
    assert response.status_code == 426
    assert json.loads(response.body)["code"] == "upgrade_required"
    assert not entered
    for header, version in protocol_headers().items():
        assert response.headers[header] == version


def test_real_websocket_success_declares_versions(protocol_server):
    url, entered = protocol_server
    with connect(url, additional_headers=protocol_headers(), proxy=None) as ws:
        assert ws.recv(timeout=5) == "accepted"
        for header, version in protocol_headers().items():
            assert ws.response.headers[header] == version
    assert entered == [True]
