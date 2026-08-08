import argparse
import sys

from rc2014bridge.api import ApiServer
from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink

DEFAULT_SOCK = "/tmp/rc2014bridge.sock"


def main():
    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI + control API")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=24)
    p.add_argument("--sock", default=DEFAULT_SOCK)
    args = p.parse_args()

    link = SerialLink(args.port, baud=args.baud, cols=args.cols, rows=args.rows)
    api = ApiServer(link, args.sock)
    api.start()
    print(f"API listening on {args.sock}", file=sys.stderr)

    try:
        run_display(link)
    finally:
        api.stop()
        link.close()


if __name__ == "__main__":
    main()
