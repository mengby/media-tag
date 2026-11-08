"""Private JSON-line CPU encoder protocol, watched by the lease-owning parent."""
import argparse
import json
from pathlib import Path
import sys
import time
from media_tag.models.contracts import ModelFailure
from media_tag.models.lifecycle import watch_parent
from .registry import BGERegistry


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--parent',type=int,required=True)
    parser.add_argument('--revision',required=True)
    args=parser.parse_args();watch_parent(args.parent)
    registry=BGERegistry(args.root);model=None
    for line in sys.stdin.buffer:
        if len(line)>256*1024:return 2
        request={};started=time.monotonic()
        try:
            request=json.loads(line)
            if set(request)!={'id','texts','query'} or type(request['query']) is not bool:raise ModelFailure('text_protocol')
            if registry.revision!=args.revision:raise ModelFailure('text_model_revision')
            if model is None:
                from .model import BGEModel
                model=BGEModel(registry)
            vectors=model.encode(request['texts'],query=request['query'])
            import psutil
            memory=psutil.Process().memory_info()
            result={'id':request['id'],'ok':True,'vectors':vectors,'model_revision':registry.revision,'device':'cpu',
                    'elapsed_seconds':time.monotonic()-started,'peak_process_bytes':getattr(memory,'peak_wset',memory.rss)}
        except Exception as exc:
            result={'id':request.get('id'),'ok':False,'error':exc.code if isinstance(exc,ModelFailure) else 'text_model_dependency_unavailable' if isinstance(exc,ImportError) else 'text_model_execution_failed'}
        sys.stdout.write(json.dumps(result,allow_nan=False,separators=(',',':'))+'\n');sys.stdout.flush()
    return 0


if __name__=='__main__':raise SystemExit(main())
