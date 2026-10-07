"""k3code gateway: JSON-RPC 2.0 stdio server speaking the TUI gateway protocol."""

from k3code.gateway.server import GatewayServer, main

__all__ = ["GatewayServer", "main"]
