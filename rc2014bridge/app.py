import argparse
import logging
import sys

from rc2014bridge import config as bridge_config
from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink
from rc2014bridge.mcp_server import McpServer

DEFAULT_LOG = "rc2014bridge.log"
DEFAULT_HW_INFO = "hardware_info.json"


def _cfg_str(cfg: dict, key: str, fallback):
    val = cfg.get(key)
    return val if val else fallback


def _cfg_int(cfg: dict, key: str, fallback: int) -> int:
    val = cfg.get(key)
    if not val:
        return fallback
    try:
        return int(val)
    except ValueError:
        print(f"Config: {key}={val!r} is not an integer, using default {fallback}", file=sys.stderr)
        return fallback


def _cfg_bool(cfg: dict, key: str, fallback: bool) -> bool:
    val = cfg.get(key)
    if not val:
        return fallback
    return val.strip().lower() in ("1", "true", "yes", "on")


def main():
    # First pass just locates --config, so its contents can seed every other
    # flag's default before the real parser (with --help, choices, etc.) is
    # built. A flag given on the actual command line always wins over
    # whatever the config file says - see the p.add_argument() calls below,
    # which only use the config file to fill in `default=`.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=bridge_config.DEFAULT_CONFIG_PATH)
    pre_args, _ = pre.parse_known_args()
    cfg = bridge_config.load(pre_args.config)

    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI terminal + MCP server")
    p.add_argument("--config", default=pre_args.config,
                    help="Path to an INI config file providing defaults for the flags below "
                         "(default: %(default)s; a missing file just means the built-in "
                         "defaults apply). Any flag given on the command line overrides it.")
    p.add_argument("--port", default=_cfg_str(cfg, "port", "/dev/ttyUSB0"))
    p.add_argument("--baud", type=int, default=_cfg_int(cfg, "baud", 115200))
    # 160 columns is twice the console's own 80, which makes for a comfortably
    # wide window without shrinking the terminal or growing its height. The board
    # still emits 80-column output; the extra width just isn't used by it.
    p.add_argument("--cols", type=int, default=_cfg_int(cfg, "cols", 160))
    p.add_argument("--rows", type=int, default=_cfg_int(cfg, "rows", 48))
    p.add_argument("--log-file", default=_cfg_str(cfg, "log-file", DEFAULT_LOG), help="Path to log file")
    p.add_argument("--hw-info", default=_cfg_str(cfg, "hw-info", DEFAULT_HW_INFO),
                   help="Path to the captured hardware info JSON (default: %(default)s)")
    p.add_argument("--mcp", action="store_true", default=_cfg_bool(cfg, "mcp", True),
                   help="Enable the MCP server (default: True)")
    p.add_argument("--no-mcp", dest="mcp", action="store_false", help="Disable the MCP server")
    p.add_argument("--mcp-host", default=_cfg_str(cfg, "mcp-host", "0.0.0.0"),
                   help="Host IP to bind the MCP server (default: %(default)s)")
    p.add_argument("--mcp-port", type=int, default=_cfg_int(cfg, "mcp-port", 8014),
                   help="Port for the MCP server (default: %(default)s)")
    p.add_argument("--mcp-transport", choices=["http", "sse", "both"],
                   default=_cfg_str(cfg, "mcp-transport", "both"),
                   help="Serve streamable HTTP at /mcp, the older SSE at /sse, or both "
                        "(default: %(default)s)")
    p.add_argument("--xmodem-pacing", metavar="CHUNK:DELAY_MS", default=_cfg_str(cfg, "xmodem-pacing", None),
                   help="Override XMODEM write pacing, e.g. 128:2. Default: whatever "
                        "rc2014_calibrate_pacing last proved on this machine, else 8:10")
    p.add_argument("--text-pacing", metavar="CHUNK:DELAY_MS", default=_cfg_str(cfg, "text-pacing", None),
                   help="Override keystroke/command write pacing, e.g. 8:5 (default 1:15)")
    p.add_argument("--rtscts", action="store_true", default=_cfg_bool(cfg, "rtscts", False),
                   help="Enable hardware RTS/CTS flow control on the serial port. "
                        "Experimental: only meaningful if both the board and the cable "
                        "actually wire CTS/RTS through (default: off)")
    p.add_argument("--verbose", "-v", action="store_true", default=_cfg_bool(cfg, "verbose", False),
                   help="Enable DEBUG level logging")
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
                      xmodem_pacing=_pacing(args.xmodem_pacing),
                      rtscts=args.rtscts)

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
        run_display(link, config_path=args.config)
    finally:
        if mcp_server:
            mcp_server.stop()
        link.close()
        logging.info("RC2014 Bridge shutdown complete")


if __name__ == "__main__":
    main()
