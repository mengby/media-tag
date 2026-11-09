"""Explicit optional CLI for portable publication; never reads implicit local config."""
import argparse
from pathlib import Path
from media_contracts import canonical


def main(argv=None):
    parser = argparse.ArgumentParser(prog="media-tag publish")
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("build")
    build.add_argument("--input", type=Path, required=True)
    build.add_argument("--destination", type=Path, required=True)
    push = commands.add_parser("push")
    push.add_argument("--bundle", type=Path, required=True)
    push.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "build":
        from .bundle import ExportSettings, build_bundle
        result = build_bundle(ExportSettings.model_validate_json(args.input.read_bytes()), args.destination)
        print(canonical({"release_id": result.release_id, "objects": len(result.objects), "state": "built"}).decode())
    else:
        from .client import PublishSettings, push_bundle
        result = push_bundle(args.bundle, PublishSettings.model_validate_json(args.config.read_bytes()))
        print(canonical(result).decode())
    return 0
