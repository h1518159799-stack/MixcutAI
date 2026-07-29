from __future__ import annotations

import argparse
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commercial_api import CommercialHandler, prepare


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    prepare()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CommercialHandler)
    print(f"http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
