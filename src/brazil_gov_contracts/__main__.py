import os
from .mcp_server import create_server


def main() -> None:
    server = create_server()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server.settings.host = host
    server.settings.port = port
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
