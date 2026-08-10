import argparse
import logging
import sys

from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink
from rc2014bridge.mcp_server import McpServer

DEFAULT_LOG = "rc2014bridge.log"
DEFAULT_HW_INFO = "hardware_info.json"


def main():
    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI terminal + MCP server")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    # 160 columns is twice the console's own 80, which makes for a comfortably
    # wide window without shrinking the terminal or growing its height. The board
    # still emits 80-column output; the extra width just isn't used by it.
    p.add_argument("--cols", type=int, default=160)
    p.add_argument("--rows", type=int, default=48)
    p.add_argument("--log-file", default=DEFAULT_LOG, help="Path to log file")
    p.add_argument("--hw-info", default=DEFAULT_HW_INFO,
                   help="Path to the captured hardware info JSON (default: %(default)s)")
    p.add_argument("--mcp", action="store_true", default=True, help="Enable the MCP server (default: True)")
    p.add_argument("--no-mcp", dest="mcp", action="store_false", help="Disable the MCP server")
    p.add_argument("--mcp-host", default="0.0.0.0", help="Host IP to bind the MCP server (default: %(default)s)")
    p.add_argument("--mcp-port", type=int, default=8014, help="Port for the MCP server (default: %(default)s)")
    p.add_argument("--mcp-transport", choices=["http", "sse", "both"], default="both",
                   help="Serve streamable HTTP at /mcp, the older SSE at /sse, or both "
                        "(default: %(default)s)")
    p.add_argument("--xmodem-pacing", metavar="CHUNK:DELAY_MS",
                   help="Override XMODEM write pacing, e.g. 128:2. Default: whatever "
                        "rc2014_calibrate_pacing last proved on this machine, else 8:10")
    p.add_argument("--text-pacing", metavar="CHUNK:DELAY_MS",
                   help="Override keystroke/command write pacing, e.g. 8:5 (default 1:15)")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG level logging")
    args = p.parse_args()

    def _pacing(value):
        if not value:
            return None
        chunk, _, delay_ms = value.partition(":")
        return int(chunk), float(delay_ms) / 1000.0

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

    link = SerialLink(args.port, baud=args.baud, cols=args.cols, rows=args.rows,
                      hw_info_file=args.hw_info,
                      text_pacing=_pacing(args.text_pacing),
                      xmodem_pacing=_pacing(args.xmodem_pacing))

    mcp_server = None
    if args.mcp:
        try:
            mcp_server = McpServer(link, host=args.mcp_host, port=args.mcp_port,
                                   transport=args.mcp_transport)
            mcp_server.start()
            for url in mcp_server.endpoints():
                logging.info("MCP endpoint: %s", url)
        except Exception as e:
            logging.warning("Failed to start MCP server: %s", e)

    try:
        run_display(link)
    finally:
        if mcp_server:
            mcp_server.stop()
        link.close()
        logging.info("RC2014 Bridge shutdown complete")


if __name__ == "__main__":
    main()
