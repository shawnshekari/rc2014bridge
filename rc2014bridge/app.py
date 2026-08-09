import argparse
import logging
import sys

from rc2014bridge.api import ApiServer
from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink

DEFAULT_SOCK = "/tmp/rc2014bridge.sock"
DEFAULT_LOG = "rc2014bridge.log"


def main():
    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI + control API")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=24)
    p.add_argument("--sock", default=DEFAULT_SOCK)
    p.add_argument("--log-file", default=DEFAULT_LOG, help="Path to log file")
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

    try:
        run_display(link)
    finally:
        api.stop()
        link.close()
        logging.info("RC2014 Bridge shutdown complete")


if __name__ == "__main__":
    main()
