"""Read-only capacity accounting and bounded diagnostic statistics; no cleanup or quotas."""
from math import ceil,isfinite
import os
from pathlib import Path
import stat


def request_summary(records):
    """Nearest-rank percentiles include failed and timed-out request durations."""
    records=list(records)
    if not records:
        return {'count':0,'errors':0,'timeouts':0,'error_rate':None,'p50_ms':None,'p95_ms':None,'max_ms':None}
    values=[]
    for item in records:
        value=item['elapsed_ms']
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or value<0:
            raise ValueError('finite nonnegative request duration required')
        values.append(value)
    values.sort();errors=sum(not item['ok'] for item in records)
    return {'count':len(records),'errors':errors,'timeouts':sum(bool(item.get('timeout')) for item in records),
            'error_rate':errors/len(records),'p50_ms':values[ceil(.5*len(values))-1],
            'p95_ms':values[ceil(.95*len(values))-1],'max_ms':values[-1]}


def browser_summary(report):
    """Page initialization and first query are different cold measurements."""
    return {'warm':request_summary(report['rows']),
            'cold_page':request_summary(r for r in report['cold'] if r.get('kind')!='first_search'),
            'cold_query':request_summary(r for r in report['cold'] if r.get('kind')=='first_search')}


def directory_usage(root):
    """Logical bytes, unique file identities, no directory links/reparse traversal."""
    root=Path(root);pending=[root];seen=set();size=files=skipped=unreadable=0
    if not root.exists():return {'exists':False,'logical_bytes':0,'files':0,'skipped_links':0,'unreadable':0}
    while pending:
        path=pending.pop()
        try:
            info=path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
                skipped+=1;continue
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:pending.extend(Path(entry.path) for entry in entries)
            elif stat.S_ISREG(info.st_mode):
                identity=(info.st_dev,info.st_ino)
                if identity not in seen:seen.add(identity);size+=info.st_size;files+=1
        except OSError:unreadable+=1
    return {'exists':True,'logical_bytes':size,'files':files,'skipped_links':skipped,'unreadable':unreadable}


def capacity_projection(source_bytes,objects_bytes,workspace_bytes):
    if source_bytes<=0 or min(objects_bytes,workspace_bytes)<0:raise ValueError('measured source bytes required')
    live=objects_bytes+workspace_bytes
    # Explicit scenario: one retained input staging copy and one full live backup.
    # No compression, dedup, retention period, IOPS or production capacity is promised.
    return {'source_bytes':source_bytes,'objects_bytes':objects_bytes,'workspace_bytes':workspace_bytes,
            'live_to_input_ratio':live/source_bytes,'one_input_copy_and_one_live_backup_ratio':(2*live+source_bytes)/source_bytes,
            'basis':'synthetic measured profile; logical bytes; scenario, not production sizing or retention policy'}
