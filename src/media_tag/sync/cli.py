"""Explicit one-pass sync and bounded local loop; never starts source Workers."""
import argparse
import json
from pathlib import Path
import time
from .bridge import Bridge,Settings


def main(argv=None):
    parser=argparse.ArgumentParser(prog='media-tag sync')
    parser.add_argument('action',choices=('once','loop','status'))
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=60)
    args=parser.parse_args(argv)
    if not 1<=args.seconds<=3600:parser.error('seconds must be 1..3600')
    bridge=None
    try:
        with args.config.open('rb') as stream:raw=stream.read(262145)
        if len(raw)>262144:raise ValueError('sync config budget')
        bridge=Bridge(Settings.model_validate_json(raw));end=time.monotonic()+args.seconds
        while True:
            result=bridge.state.summary() if args.action=='status' else bridge.once()
            print(json.dumps(result,ensure_ascii=False))
            if args.action!='loop' or time.monotonic()>=end:break
            time.sleep(min(2,max(0,end-time.monotonic())))
        return 0
    except Exception as exc:
        print(json.dumps({'error':getattr(exc,'code','sync_configuration_or_transport_failure')}));return 2
    finally:
        if bridge:bridge.close()
