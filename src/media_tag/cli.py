"""Project command router. Media analysis remains a trusted Python API until D03."""

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "contracts":
        from .contracts.cli import main as contracts_main
        return contracts_main(argv)
    parser = argparse.ArgumentParser(prog="media-tag")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("contracts", help="local contract checks and schemas")
    tools = commands.add_parser("check-media-tools", help="check configured FFmpeg/ffprobe hashes and versions")
    tools.add_argument("--tool-dir", type=Path)
    args = parser.parse_args(argv)
    from .workers.probe import MediaError, Toolchain
    try:
        print(json.dumps(Toolchain(args.tool_dir).identity, ensure_ascii=True, indent=2))
        return 0
    except MediaError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 2
