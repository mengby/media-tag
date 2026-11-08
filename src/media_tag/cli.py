"""Explicit project commands; services never start on import."""

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
    term_schema = commands.add_parser('terminology-schema', help='print strict D13 contracts; no operational data')
    term_schema.add_argument('--model', choices=('taxonomy','lexicon','candidate','revision','review','publish','rollback','import'), required=True)
    tools = commands.add_parser("check-media-tools", help="check configured FFmpeg/ffprobe hashes and versions")
    tools.add_argument("--tool-dir", type=Path)
    serve = commands.add_parser("serve", help="start the authenticated loopback Router API")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8000)
    worker = commands.add_parser("work-once", help="execute at most one queued local stage, including explicitly enabled models")
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--worker-id", required=True)
    for name in ('prepare-models', 'check-models'):
        model_command = commands.add_parser(name, help='explicit D04 model download or GPU compatibility check')
        model_command.add_argument('--model-root', type=Path, default=Path('cache/models/d04'))
    args = parser.parse_args(argv)
    if args.command == 'terminology-schema':
        from .terminology.contracts import Taxonomy,Lexicon,CandidateSubmission,CandidateRevision,Review,Publish,Rollback,ImportRows
        models=dict(zip(('taxonomy','lexicon','candidate','revision','review','publish','rollback','import'),
                        (Taxonomy,Lexicon,CandidateSubmission,CandidateRevision,Review,Publish,Rollback,ImportRows)))
        print(json.dumps(models[args.model].model_json_schema(), ensure_ascii=True, indent=2))
        return 0
    if args.command in ('prepare-models', 'check-models'):
        from .models.registry import ModelRegistry
        from .models.contracts import ModelFailure
        try:
            registry = ModelRegistry(args.model_root)
            if args.command == 'prepare-models':
                registry.fetch()
                result = {'model_revision': registry.revision, 'weights_verified': True}
            else:
                from .models.inference import gpu_probe
                for kind in ('ocr', 'face', 'clip'):
                    registry.verify(kind)
                result = {'model_revision': registry.revision, 'weights_verified': True, 'gpu': gpu_probe()}
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({'error': exc.code if isinstance(exc, ModelFailure) else 'model_check_failed'}), file=sys.stderr)
            return 2
    if args.command in ("serve", "work-once"):
        from .api.config import executor_from_config, load_application
        try:
            if args.command == "work-once":
                outcome = executor_from_config(args.config, args.worker_id).run_once()
                print(json.dumps({"outcome": outcome}, ensure_ascii=True))
                return 0 if outcome is None or outcome["accepted"] else 2
            if not 1 <= args.port <= 65535:
                raise ValueError("invalid port")
            import uvicorn
            app, _, _ = load_application(args.config)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
            return 0
        except Exception:
            # Configuration/validation errors can contain local credentials; report a bounded code only.
            print(json.dumps({"error": "local_runtime_failed"}), file=sys.stderr)
            return 2
    from .workers.probe import MediaError, Toolchain
    try:
        print(json.dumps(Toolchain(args.tool_dir).identity, ensure_ascii=True, indent=2))
        return 0
    except MediaError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 2
