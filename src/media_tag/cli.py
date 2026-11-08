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
    editing=commands.add_parser('editing-source',help='explicit new editing jobs and external media-tag acquisition storage')
    editing.add_argument('action',choices=('init-job','search','fetch','validate'))
    editing.add_argument('--config',type=Path,required=True)
    editing.add_argument('--job-id',required=True)
    editing.add_argument('--input',type=Path,help='bounded JSON file; otherwise JSON from stdin')
    term_schema = commands.add_parser('terminology-schema', help='print strict D13 contracts; no operational data')
    term_schema.add_argument('--model', choices=('taxonomy','lexicon','candidate','revision','review','publish','rollback','import'), required=True)
    mcp_command=commands.add_parser('mcp-stdio',help='run the optional read-only MCP stdio bridge with a private client configuration')
    mcp_command.add_argument('--config',type=Path,required=True)
    for name in ('acquire-media','prepare-preview-handoff'):
        task_command=commands.add_parser(name,help='fixed media-tag bytes or checked preview handoff in an explicitly owned new job')
        task_command.add_argument('--config',type=Path,required=True)
        task_command.add_argument('--jobs-root',type=Path,required=True)
        task_command.add_argument('--job-id',required=True)
        task_command.add_argument('--input',type=Path,required=True)
        task_command.add_argument('--tool-dir',type=Path)
        task_command.add_argument('--license-validation',choices=('enabled','disabled'),default='enabled')
    backup_command=commands.add_parser('backup-offline',help='snapshot an explicitly stopped service; never starts or stops processes')
    backup_command.add_argument('--config',type=Path,required=True)
    backup_command.add_argument('--destination',type=Path,required=True)
    backup_command.add_argument('--offline-confirmed',action='store_true')
    for name in ('verify-backup','restore-offline'):
        recovery=commands.add_parser(name,help='verify a pinned private backup or restore into a fresh isolated directory')
        recovery.add_argument('--source',type=Path,required=True)
        recovery.add_argument('--manifest-sha256',required=True)
        if name=='restore-offline':recovery.add_argument('--destination',type=Path,required=True)
    for name in ('local-init','local-run','local-status','local-stop','local-child'):
        local=commands.add_parser(name,help='explicit current-user Windows loopback deployment management')
        local.add_argument('--root',type=Path,required=True)
        if name=='local-init':
            local.add_argument('--project',type=Path,required=True)
            local.add_argument('--port',type=int,default=8765)
            local.add_argument('--without-vision',action='store_true')
        if name=='local-child':
            local.add_argument('--generation',required=True)
            local.add_argument('--kind',choices=('api','worker'),required=True)
    for name in ('prepare-text-model','check-text-model'):
        text_command=commands.add_parser(name,help='explicit pinned BGE download or real CPU encoding diagnostic')
        text_command.add_argument('--model-root',type=Path,default=Path('cache/models/d14'))
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
    if args.command=='editing-source':
        from .client.editing import execute,read
        from .client.http import LibraryClientError
        try:
            value=None
            if args.action!='init-job':
                raw=read(args.input) if args.input else sys.stdin.buffer.read(4*1024**2+1)
                if len(raw)>4*1024**2:raise LibraryClientError('editing_input_byte_budget')
                value=json.loads(raw)
            result=execute(args.action,args.config,args.job_id,value)
            print(json.dumps(result,ensure_ascii=True));return 0
        except Exception as exc:
            print(json.dumps(exc.payload() if isinstance(exc,LibraryClientError) else {'error':{'code':'editing_source_failed','retryable':False}}),file=sys.stderr)
            return 2
    if args.command.startswith('local-'):
        from .deployment.local import DeploymentError,initialize,supervise,status,stop,child
        try:
            if args.command=='local-run':return supervise(args.root)
            if args.command=='local-child':child(args.root,args.generation,args.kind);return 0
            if args.command=='local-init':result=initialize(args.root,args.project,port=args.port,vision=not args.without_vision)
            elif args.command=='local-status':result=status(args.root)
            else:result=stop(args.root)
            if sys.stdout is not None:print(json.dumps(result,ensure_ascii=True))
            return 0
        except Exception as exc:
            if sys.stderr is not None:print(json.dumps({'error':str(exc) if isinstance(exc,DeploymentError) else 'local_deployment_failed'}),file=sys.stderr)
            return 2
    if args.command in ('backup-offline','verify-backup','restore-offline'):
        from .maintenance.backup import RecoveryError,snapshot,restore,verify_backup
        try:
            if args.command=='backup-offline':
                from .api.config import LocalConfig
                with args.config.open('rb') as stream:raw=stream.read(1024**2+1)
                if len(raw)>1024**2:raise RecoveryError('private_config_byte_budget')
                config=LocalConfig.model_validate_json(raw)
                result=snapshot(Path(config.database),Path(config.workspace),Path(config.object_store),args.destination,offline_confirmed=args.offline_confirmed)
            elif args.command=='restore-offline':result=restore(args.source,args.destination,args.manifest_sha256)
            else:
                _,manifest=verify_backup(args.source,args.manifest_sha256)
                result={'manifest_sha256':args.manifest_sha256,'files':len(manifest.files),'database_integrity':'ok','state':'verified'}
            print(json.dumps(result,ensure_ascii=True));return 0
        except Exception as exc:
            print(json.dumps({'error':str(exc) if isinstance(exc,RecoveryError) else 'offline_maintenance_failed'}),file=sys.stderr);return 2
    if args.command in ('acquire-media','prepare-preview-handoff'):
        from .client.http import LibraryClient,LibraryClientError,load_settings
        from .client.models import Acquisition,CurrentUseCheck
        from .client.provider import TaskProvider
        from .workers.probe import Toolchain,MediaError
        try:
            with args.input.open('rb') as stream:raw=stream.read(4*1024**2+1)
            if len(raw)>4*1024**2:raise LibraryClientError('task_input_byte_budget')
            model=Acquisition if args.command=='acquire-media' else CurrentUseCheck
            spec=model.model_validate_json(raw)
            provider=TaskProvider(LibraryClient(load_settings(args.config)),args.jobs_root,args.job_id,Toolchain(args.tool_dir),license_validation=args.license_validation=='enabled')
            result=provider.fetch(spec) if args.command=='acquire-media' else provider.prepare_preview(spec)
            print(json.dumps(result,ensure_ascii=True));return 0
        except Exception as exc:
            code=exc.code if isinstance(exc,(LibraryClientError,MediaError)) else 'task_adapter_failed'
            print(json.dumps({'error':{'code':code,'retryable':getattr(exc,'retryable',False)}}),file=sys.stderr);return 2
    if args.command=='mcp-stdio':
        try:
            import asyncio
            from .client.http import load_settings
            from .mcp.server import serve
            asyncio.run(serve(load_settings(args.config)));return 0
        except Exception:
            print(json.dumps({'error':'mcp_bridge_failed'}),file=sys.stderr);return 2
    if args.command in ('prepare-text-model','check-text-model'):
        from .terminology.embedding.registry import BGERegistry
        from .terminology.embedding.process import ProcessEncoder
        from .models.contracts import ModelFailure
        registry=BGERegistry(args.model_root)
        encoder=None
        try:
            if args.command=='prepare-text-model':result=registry.fetch()
            else:
                registry.verify();encoder=ProcessEncoder(registry,Path.cwd())
                docs=encoder.encode(['红色的圆形','蓝色的方形'],timeout_seconds=30.0)
                query=encoder.encode(['红色圆圈'],query=True,timeout_seconds=8.0)[0]
                result={'model_revision':registry.revision,'upstream_revision':registry.manifest['upstream_revision'],'dimension':len(query),
                    'diagnostic_cosines':[sum(a*b for a,b in zip(query,v)) for v in docs],'stats':encoder.stats,
                    'medical_validation':False,'device':'cpu'}
            print(json.dumps(result,ensure_ascii=True,indent=2));return 0
        except Exception as exc:
            print(json.dumps({'error':exc.code if isinstance(exc,ModelFailure) else 'text_model_check_failed'}),file=sys.stderr);return 2
        finally:
            if encoder:encoder.close()
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
