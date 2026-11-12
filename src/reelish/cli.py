"""The small command-line entry point."""

import argparse
import os
from pathlib import Path

from . import __version__


def main():
    parser = argparse.ArgumentParser(
        description="Save a movie. Play it by almost-name."
    )
    parser.add_argument("--version", action="version", version="reelish " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="start the download queue and streaming server"
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    serve.add_argument("--port", type=int, default=os.environ.get("PORT", "8766"))
    serve.add_argument(
        "--movies", type=Path, help="media directory (default: ~/Movies)"
    )
    serve.add_argument("--state", type=Path, help="queue state directory")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    from . import api

    if args.movies is not None:
        api.ROOT = args.movies.expanduser().resolve()
    if args.state is not None:
        api.STATE = args.state.expanduser().resolve()
    if len(api.TOKEN) < 32:
        parser.error("Set REELISH_TOKEN to a random token of at least 32 characters")
    api.serve(args.host, args.port)
