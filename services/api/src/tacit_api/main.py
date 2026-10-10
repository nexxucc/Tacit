import uvicorn

from tacit_api.app import Settings, create_app


def main() -> None:
    settings = Settings()
    if settings.demo and settings.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Demo mode must bind to loopback")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
