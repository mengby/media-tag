import argparse
import json
import sys
from pathlib import Path
from . import Dataset,Export,evaluate,propose_rules
from .io import publish


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='Offline source/group isolated ten-field evaluation; no model calls.')
    parser.add_argument('--schema',choices=('dataset','export'))
    parser.add_argument('--dataset');parser.add_argument('--export');parser.add_argument('--output')
    parser.add_argument('--split',choices=('calibration','acceptance'),default='acceptance')
    parser.add_argument('--propose-rules',action='store_true')
    args=parser.parse_args()
    if args.schema:
        print(json.dumps((Dataset if args.schema=='dataset' else Export).model_json_schema(),ensure_ascii=False,indent=2));return 0
    if not args.dataset or not args.export or not args.output:parser.error('--dataset --export --output required')
    if args.propose_rules and args.split!='calibration':parser.error('--propose-rules requires --split calibration')
    result=evaluate(args.dataset,args.export,split=args.split,output_directory=args.output)
    print(json.dumps({'report_file':str(result['path']),'report_sha256':result['sha256'],
        'calibrated':False,'business_acceptance':False},ensure_ascii=False))
    if args.propose_rules:
        path,digest=publish(args.output,propose_rules(result['report']))
        print(json.dumps({'candidate_file':str(path),'sha256':digest,'automatic_enablement':False},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
