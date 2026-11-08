import csv
from datetime import datetime,timezone
from hashlib import sha256
from io import StringIO
import os
from pathlib import Path
import stat
from uuid import uuid4
from media_tag.catalog.service import encode
from media_tag.deployment.local import directory,write_json
from media_tag.files.service import media_type
from media_tag.storage.filesystem import FilesystemBackend,_linked
from media_tag.storage import StorageError
from media_tag.upload.media import EXTENSIONS
from .models import Inventory,InventoryEntry,IntakeError


LIMIT=64*1024**2


def file_identity(info):return info.st_size,info.st_mtime_ns,info.st_dev,info.st_ino


def hash_stream(stream):
    stream.seek(0);digest=sha256()
    while chunk:=stream.read(1024**2):digest.update(chunk)
    return digest.hexdigest()


def sensitive(name):
    value=name.casefold()
    return value in ('.git','.aws','.codex','.agents','node_modules','$recycle.bin','system volume information') or value=='.env' or value.startswith('.env.') or value.endswith(('.local.json','.local.txt'))


def csv_bytes(rows,columns):
    stream=StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
    for row in rows:
        writer.writerow({key:("'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value) for key,value in row.items() if key in columns})
    return ('\ufeff'+stream.getvalue()).encode('utf8')


def summary(inventory):
    rows=inventory.entries;ready=[r for r in rows if r.state=='ready']
    return {'batch_id':inventory.batch_id,'source_files':len(rows),'ready':len(ready),
        'unsupported':sum(r.state=='unsupported' for r in rows),'excluded':sum(r.state=='excluded' for r in rows),
        'failed':sum(r.state=='failed' for r in rows),'source_bytes':sum(r.size_bytes for r in ready),
        'unique_sha256':len({r.sha256 for r in ready}),'duplicate_paths':sum(r.duplicate_of is not None for r in ready)}


def scan(source,jobs_root,*,max_files=10000,max_file_bytes=2*1024**3):
    source=directory(source);jobs_root=directory(jobs_root)
    if source.is_relative_to(jobs_root) or jobs_root.is_relative_to(source):raise IntakeError('source_and_batch_roots_overlap')
    if not 1<=max_files<=100000 or not 1<=max_file_bytes<=2*1024**3:raise IntakeError('invalid_inventory_budget')
    backend=FilesystemBackend(source);pending=[source];rows=[];hashes={};visited=0
    while pending:
        parent=pending.pop()
        # Resolve each directory from the original root without following reparse points.
        if parent!=source:
            part=source
            for name in parent.relative_to(source).parts:
                part=part/name
                if _linked(part):raise IntakeError('inventory_directory_changed')
        try:
            with os.scandir(parent) as entries:children=sorted(entries,key=lambda e:e.name.casefold())
        except OSError:
            if parent==source:raise IntakeError('source_directory_unreadable') from None
            rows.append(InventoryEntry(key=parent.relative_to(source).as_posix(),state='failed',error_code='directory_unreadable'));continue
        for child in children:
            visited+=1
            if visited>max_files:raise IntakeError('inventory_entry_budget')
            path=Path(child.path);key=path.relative_to(source).as_posix()
            try:
                # Windows DirEntry caches may report st_dev/st_ino as zero.
                # Use an actual path stat before comparing against the opened handle.
                info=path.lstat()
                if sensitive(child.name) or _linked(path):
                    rows.append(InventoryEntry(key=key,state='excluded',error_code='private_or_linked_path'));continue
                if stat.S_ISDIR(info.st_mode):pending.append(path);continue
                if not stat.S_ISREG(info.st_mode):
                    rows.append(InventoryEntry(key=key,state='excluded',error_code='not_regular_file'));continue
                if path.suffix.lower() not in EXTENSIONS:
                    rows.append(InventoryEntry(key=key,state='unsupported',size_bytes=info.st_size,error_code='unsupported_extension'));continue
                if not 0<info.st_size<=max_file_bytes:
                    rows.append(InventoryEntry(key=key,state='failed',size_bytes=info.st_size,error_code='source_size_budget'));continue
                with backend.open_read(key) as stream:
                    before=os.fstat(stream.fileno());hint=media_type(stream);digest=hash_stream(stream);after=os.fstat(stream.fileno())
                    if file_identity(before)!=file_identity(after) or file_identity(info)!=file_identity(before):raise IntakeError('source_changed_during_scan')
                duplicate=hashes.get(digest);hashes.setdefault(digest,key)
                rows.append(InventoryEntry(key=key,state='ready',size_bytes=info.st_size,sha256=digest,mtime_ns=info.st_mtime_ns,
                    device=info.st_dev,inode=info.st_ino,signature_hint=hint,duplicate_of=duplicate))
            except (OSError,ValueError,StorageError,IntakeError) as exc:
                # File-level failures are evidence, not a reason to invent or skip a record.
                rows.append(InventoryEntry(key=key,state='failed',error_code=exc.code if isinstance(exc,IntakeError) else 'source_unreadable_or_unsafe'))
    inventory=Inventory(batch_id='intake-'+uuid4().hex[:12],source_root=str(source),created_at=datetime.now(timezone.utc),entries=tuple(sorted(rows,key=lambda r:r.key)))
    raw=encode(inventory.model_dump(mode='json')).encode()
    if len(raw)>LIMIT:raise IntakeError('inventory_manifest_byte_budget')
    batch=jobs_root/inventory.batch_id;batch.mkdir()
    (batch/'README.md').write_text('Local read-only source inventory and explicit authenticated batch intake. Original bytes/names stay unchanged. Import is private AI draft, not medical/privacy/license approval. Inventory JSON is pinned; progress is resumable.\n',encoding='utf8')
    write_json(batch,'inventory.json',inventory.model_dump(mode='json'))
    digest=sha256((batch/'inventory.json').read_bytes()).hexdigest()
    write_json(batch,'progress.json',{'schema_version':'local-intake-progress-v1','inventory_sha256':digest,'state':'inventoried','rows':{}})
    (batch/'inventory.csv').write_bytes(csv_bytes([r.model_dump() for r in inventory.entries],list(InventoryEntry.model_fields)))
    write_json(batch,'summary.json',summary(inventory))
    return batch,summary(inventory)


def load_batch(batch):
    batch=directory(batch);backend=FilesystemBackend(batch)
    with backend.open_read('inventory.json') as stream:raw=stream.read(LIMIT+1)
    if len(raw)>LIMIT:raise IntakeError('inventory_manifest_byte_budget')
    with backend.open_read('progress.json') as stream:progress_raw=stream.read(LIMIT+1)
    if len(progress_raw)>LIMIT:raise IntakeError('progress_byte_budget')
    import json
    progress=json.loads(progress_raw)
    if progress.get('inventory_sha256')!=sha256(raw).hexdigest():raise IntakeError('inventory_manifest_changed')
    inventory=Inventory.model_validate_json(raw)
    if inventory.batch_id!=batch.name:raise IntakeError('batch_identity_changed')
    return batch,inventory,progress
