import argparse
import logging
import sys

from rc2014bridge.api import ApiServer
from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink
from rc2014bridge.mcp_server import McpServer

DEFAULT_SOCK = "/tmp/rc2014bridge.sock"
DEFAULT_LOG = "rc2014bridge.log"


def main():
    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI + control API + MCP HTTP server")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=48)
    p.add_argument("--sock", default=DEFAULT_SOCK)
    p.add_argument("--log-file", default=DEFAULT_LOG, help="Path to log file")
    p.add_argument("--mcp", action="store_true", default=True, help="Enable MCP HTTP/SSE server (default: True)")
    p.add_argument("--no-mcp", dest="mcp", action="store_false", help="Disable MCP HTTP/SSE server")
    p.add_argument("--mcp-host", default="0.0.0.0", help="Host IP to bind MCP HTTP server (default: 0.0.0.0)")
    p.add_argument("--mcp-port", type=int, default=8014, help="Port to listen for MCP HTTP server (default: 8014)")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG level logging")
    args = p.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(args.log_file, mode="a"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    logging.info("Starting RC2014 Bridge on %s @ %d baud", args.port, args.baud)

    link = SerialLink(args.port, baud=args.baud, cols=args.cols, rows=args.rows)
    api = ApiServer(link, args.sock)
    api.start()
    logging.info("API listening on %s", args.sock)

    mcp_server = None
    if args.mcp:
        try:
            mcp_server = McpServer(link, host=args.mcp_host, port=args.mcp_port)
            mcp_server.start()
            logging.info("MCP HTTP/SSE Server running on http://%s:%d/sse", args.mcp_host, args.mcp_port)
        except Exception as e:
            logging.warning("Failed to start MCP server: %s", e)

    try:
        run_display(link)
    finally:
        if mcp_server:
            mcp_server.stop()
        api.stop()
        link.close()
        logging.info("RC2014 Bridge shutdown complete")


if __name__ == "__main__":
    main()
