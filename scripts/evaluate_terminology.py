"""Explicit D16 HTTP evaluation. Private input/output only; no production answers embedded."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from uuid import uuid4
from media_tag.catalog.service import encode
from media_tag.terminology.evaluation.runner import Client,load_dataset,validate_split,compare


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv==['--schema']:
        from media_tag.terminology.evaluation.models import Dataset
        print(json.dumps(Dataset.model_json_schema(),ensure_ascii=True,indent=2));return 0
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path)
    parser.add_argument('--snapshot',action='store_true',help='freeze current authorized pool and semantic pin; never generate answers')
    parser.add_argument('--check-dataset',action='store_true',help='validate frozen annotations and calibration separation without inference')
    parser.add_argument('--calibration',type=Path)
    parser.add_argument('--base-url')
    parser.add_argument('--token-file',type=Path)
    parser.add_argument('--calibrate-index',type=Path)
    parser.add_argument('--bundle-file',type=Path,help='private JSON mapping of three fixed D14 index filenames to frozen SHA256')
    parser.add_argument('--model-root',type=Path)
    parser.add_argument('--jobs-root',type=Path,default=Path('jobs'))
    args=parser.parse_args(argv);folder=None;encoder=None
    try:
        if args.snapshot:
            if not args.base_url or not args.token_file or any((args.dataset,args.calibration,args.calibrate_index,args.bundle_file,args.model_root,args.check_dataset)):
                raise ValueError('snapshot_arguments')
            from media_tag.terminology.evaluation.models import Pin
            from media_tag.terminology.evaluation.runner import pool
            client=Client(args.base_url,args.token_file.read_text(encoding='utf8').strip())
            versions=client.call('/search/vocabulary')['versions'];pin=Pin(**{key:versions[key] for key in Pin.model_fields})
            digest,assets=pool(client)
            if not args.jobs_root.is_dir():raise ValueError('existing_jobs_root_required')
            folder=args.jobs_root.resolve()/('d16-scope-'+uuid4().hex);folder.mkdir()
            (folder/'README.md').write_text('Private D16 authorized scope snapshot. No query annotations generated.\n',encoding='utf8')
            target=folder/'scope.json';target.write_text(encode({'pin':pin.model_dump(mode='json'),'candidate_pool_sha256':digest,
                'assets':sorted(assets.values(),key=lambda a:a['asset_id']),'annotations_generated':False}),encoding='utf8')
            print(json.dumps({'state':'frozen','scope':str(target)}));return 0
        if not args.dataset:raise ValueError('dataset_required')
        dataset,digest,raw=load_dataset(args.dataset);calibration=None;calibration_sha=None;calibration_raw=None
        if dataset.split=='evaluation':
            if not args.calibration:raise ValueError('separate_calibration_required')
            calibration,calibration_sha,calibration_raw=load_dataset(args.calibration);validate_split(dataset,calibration,calibration_sha)
        elif args.calibration:raise ValueError('calibration_run_does_not_consume_evaluation')
        if args.check_dataset:
            if any((args.base_url,args.token_file,args.calibrate_index,args.bundle_file,args.model_root)):raise ValueError('check_arguments')
            print(json.dumps({'state':'valid','dataset_sha256':digest,'cases':len(dataset.cases),'split':dataset.split,'business_acceptance':False}));return 0
        if args.calibrate_index:
            if dataset.split!='calibration' or not args.bundle_file or not args.model_root or args.base_url or args.token_file:
                raise ValueError('explicit_offline_calibration_inputs_required')
        elif not args.base_url or not args.token_file or args.bundle_file or args.model_root:raise ValueError('explicit_http_evaluation_inputs_required')
        root=args.jobs_root.resolve()
        if not root.is_dir():raise ValueError('existing_jobs_root_required')
        folder=root/('d16-evaluation-'+uuid4().hex);folder.mkdir()
        (folder/'README.md').write_text('Private D16 frozen inputs and evaluation results. Contains human queries; no automatic business acceptance.\n',encoding='utf-8')
        (folder/'dataset.json').write_bytes(raw)
        if calibration_raw is not None:(folder/'calibration.json').write_bytes(calibration_raw)
        if args.calibrate_index:
            from media_tag.terminology.evaluation.calibration import calibrate
            from media_tag.terminology.embedding.registry import BGERegistry
            from media_tag.terminology.embedding.process import ProcessEncoder
            bundle_raw=args.bundle_file.read_bytes()
            if len(bundle_raw)>4096:raise ValueError('index_checksum_byte_budget')
            registry=BGERegistry(args.model_root);registry.verify();encoder=ProcessEncoder(registry,folder)
            result=calibrate(dataset,args.calibrate_index,json.loads(bundle_raw),encoder,registry.revision)
            result['bundle_file_sha256']=sha256(bundle_raw).hexdigest()
        else:
            token=args.token_file.read_text(encoding='utf-8').strip()
            result=compare(Client(args.base_url,token),dataset,calibration=calibration,calibration_sha=calibration_sha)
        result.update(dataset_sha256=digest,interpreter=sys.executable,platform=platform.platform(),criteria=dataset.criteria.model_dump(mode='json'))
        (folder/'report.json').write_text(encode(result),encoding='utf-8')
        print(json.dumps({'state':'measured','report':str(folder/'report.json'),'business_acceptance':False}));return 0
    except Exception as exc:
        # Parser/Pydantic/network errors can contain tokens/URLs/clinical input. Keep bounded codes.
        from media_tag.terminology.evaluation.runner import EvaluationError
        from pydantic import ValidationError
        code=str(exc) if isinstance(exc,EvaluationError) else 'evaluation_failed'
        issues=[{'path':list(e['loc']),'type':e['type']} for e in exc.errors(include_input=False,include_url=False)[:100]] if isinstance(exc,ValidationError) else []
        if folder:(folder/'failure.json').write_text(encode({'state':'failed','code':code,'validation_issues':issues}),encoding='utf-8')
        print(json.dumps({'error':code,'validation_issues':issues}),file=sys.stderr);return 2
    finally:
        if encoder:encoder.close()


if __name__=='__main__':raise SystemExit(main())
