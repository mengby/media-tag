"""Offline-only snapshot and fresh-root restore, with pinned manifest and file integrity."""
from contextlib import closing
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.catalog import database as schema
from media_tag.catalog.service import encode
from media_tag.contracts.models import ContractModel,Sha256,WorkerResult,validate_relative_path
from media_tag.storage.filesystem import FilesystemBackend,_linked,_opened_path,require_local_path


class RecoveryError(Exception):pass


class BackupFile(ContractModel):
    path: str
    sha256: Sha256
    size_bytes: Annotated[int,Field(ge=0)]

    @model_validator(mode='after')
    def portable(self):
        validate_relative_path(self.path)
        if self.path!='database.sqlite3' and not self.path.startswith(('objects/objects/','workspace/jobs/')):raise ValueError('unsupported backup member')
        return self


class BackupManifest(ContractModel):
    schema_version: Literal['d07-offline-backup-v1']='d07-offline-backup-v1'
    created_at: str
    template_fingerprint: Sha256
    files: Annotated[tuple[BackupFile,...],Field(min_length=1,max_length=100000)]
    table_counts: dict[str,int]
    offline_operator_assertion: Literal[True]=True
    model_weights_included: Literal[False]=False
    templates_included: Literal[False]=False
    credentials_included: Literal[False]=False

    @model_validator(mode='after')
    def members(self):
        names=[f.path for f in self.files]
        if len(set(p.casefold() for p in names))!=len(names) or names.count('database.sqlite3')!=1:raise ValueError('unique portable members required')
        return self


def checked_root(path,*,exists=True):
    path=Path(path)
    if not path.is_absolute():raise RecoveryError('absolute_local_path_required')
    require_local_path(path)
    for parent in (path,*path.parents):
        if parent.exists() or parent.is_symlink():
            if _linked(parent):raise RecoveryError('linked_maintenance_path')
    if exists and not path.is_dir():raise RecoveryError('existing_directory_required')
    return path.resolve()


def fresh_root(path,excluded):
    path=checked_root(path,exists=False)
    if not path.parent.is_dir():raise RecoveryError('destination_parent_missing')
    if any(path.is_relative_to(p) or p.is_relative_to(path) for p in excluded):raise RecoveryError('maintenance_roots_overlap')
    if path.exists():raise RecoveryError('fresh_destination_required')
    path.mkdir()
    (path/'README.md').write_text('Private offline media-tag backup/restore. Contains sensitive business records; restrict OS access. Completion requires manifest/restore receipt verification. No templates, model weights or runtime credentials included.\n',encoding='utf8')
    return FilesystemBackend(path,writable=True)


def regular_files(backend,prefix):
    def walk(path):
        for item in sorted(path.iterdir()):
            if _linked(item):raise RecoveryError('linked_snapshot_member')
            if item.is_dir():yield from walk(item)
            elif item.is_file():yield item.relative_to(backend.root).as_posix()
            else:raise RecoveryError('nonregular_snapshot_member')
    root=backend._resolve(prefix,allow_missing=True)
    if root.exists():yield from walk(root)


def digest_file(backend,key,*,limit):
    digest=sha256();size=0
    with backend.open_read(key) as stream:
        while chunk:=stream.read(1024**2):
            size+=len(chunk)
            if size>limit:raise RecoveryError('snapshot_byte_budget')
            digest.update(chunk)
    return digest.hexdigest(),size


def copy_file(source,key,destination,target,*,limit):
    path=destination._resolve(target,allow_missing=True);path.parent.mkdir(parents=True,exist_ok=True)
    path=destination._resolve(target,allow_missing=True);digest=sha256();size=0
    with source.open_read(key) as input_stream,path.open('xb') as output:
        if _opened_path(output,path)!=path:raise RecoveryError('unsafe_snapshot_handle')
        while chunk:=input_stream.read(1024**2):
            size+=len(chunk)
            if size>limit:raise RecoveryError('snapshot_byte_budget')
            digest.update(chunk);output.write(chunk)
        output.flush();os.fsync(output.fileno())
    actual=digest.hexdigest()
    if digest_file(destination,target,limit=limit)!=(actual,size):raise RecoveryError('copied_file_integrity_failed')
    return BackupFile(path=target,sha256=actual,size_bytes=size)


def connect_readonly(path):
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
    connection.execute('PRAGMA trusted_schema=OFF');connection.execute('PRAGMA query_only=ON')
    return connection


def inspect_database(connection):
    expected=(schema.SCHEMA,schema.ROUTER_SCHEMA,schema.UPLOAD_SCHEMA,schema.REDACTION_SCHEMA,schema.LIBRARY_SCHEMA,
        schema.REVIEW_SCHEMA,schema.TERMINOLOGY_SCHEMA,schema.TEXT_INDEX_SCHEMA,schema.SEARCH_SCHEMA,schema.DRAFT_SCHEMA,schema.ANALYSIS_SCHEMA,schema.PROCESSING_SCHEMA)
    version=connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in (9,10,11,12):raise RecoveryError('unsupported_backup_schema')
    expected=expected[:version]
    migrations=dict(connection.execute('SELECT version,checksum FROM schema_migrations'))
    if migrations!={i:sha256('\n'.join(statements).encode()).hexdigest() for i,statements in enumerate(expected,1)}:raise RecoveryError('backup_migration_mismatch')
    if connection.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or connection.execute('PRAGMA foreign_key_check').fetchall():raise RecoveryError('backup_database_integrity_failed')
    if connection.execute("SELECT count(*) FROM router_tasks WHERE state IN ('leased','running')").fetchone()[0] or connection.execute('SELECT count(*) FROM router_holds').fetchone()[0]:raise RecoveryError('active_worker_leases_refused')
    if connection.execute("SELECT count(*) FROM uploads WHERE state='validating'").fetchone()[0]:raise RecoveryError('active_upload_finalization_refused')
    tables=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    if any(re.fullmatch('[a-z][a-z0-9_]*',t) is None for t in tables):raise RecoveryError('unexpected_database_table')
    fingerprint=connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()
    if fingerprint is None:raise RecoveryError('missing_template_binding')
    return {t:connection.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in tables},fingerprint[0]


def verify_references(connection,workspace,*,limit):
    references={}
    for (raw,) in connection.execute('SELECT result_json FROM router_attempts WHERE adopted_revision IS NOT NULL AND result_json IS NOT NULL'):
        result=WorkerResult.model_validate_json(raw)
        for ref in (*result.artifacts,*(e.artifact for e in result.evidence)):
            if ref.ref.startswith('jobs/'):references[ref.ref]=ref.sha256
    for table in ('terminology_releases','terminology_text_indices'):
        for (raw,) in connection.execute('SELECT bundle_json FROM '+table):
            bundle=json.loads(raw)
            for name,digest in bundle['files'].items():references[bundle['directory']+'/'+name]=digest
    for key,digest in references.items():
        if digest_file(workspace,key,limit=limit)[0]!=digest:raise RecoveryError('registered_artifact_integrity_failed')


def verify_objects(connection,objects,*,limit):
    for digest,size in connection.execute('SELECT sha256,size_bytes FROM blobs'):
        key=f'objects/{digest[:2]}/{digest}'
        if not connection.execute("SELECT 1 FROM storage_locations WHERE blob_sha256=? AND backend_id='local' AND object_key=?",(digest,key)).fetchone():raise RecoveryError('all_blobs_require_local_verified_copy')
        if digest_file(objects,key,limit=limit)!=(digest,size):raise RecoveryError('backup_object_integrity_failed')


def snapshot(database,workspace,object_store,destination,*,offline_confirmed=False,max_bytes=50*1024**3,max_files=100000):
    if offline_confirmed is not True:raise RecoveryError('stop_api_and_workers_before_backup')
    if type(max_bytes) is not int or max_bytes<1 or type(max_files) is not int or not 1<=max_files<=100000:raise RecoveryError('invalid_snapshot_budget')
    database=Path(database);db_parent=checked_root(database.parent);workspace=checked_root(workspace);objects=checked_root(object_store)
    db_backend=FilesystemBackend(db_parent);db_backend._resolve(database.name)
    if any(a.is_relative_to(b) or b.is_relative_to(a) for a,b in ((workspace,objects),(database.resolve(),workspace),(database.resolve(),objects))):raise RecoveryError('maintenance_roots_overlap')
    output_path=checked_root(destination,exists=False)
    if any(output_path.is_relative_to(p) or p.is_relative_to(output_path) for p in (workspace,objects,database.resolve())):raise RecoveryError('maintenance_roots_overlap')
    source_workspace=FilesystemBackend(workspace);source_objects=FilesystemBackend(objects)
    # Hold a separate writer reservation, then use SQLite's backup API on a read-only connection.
    with db_backend.open_read(database.name),closing(sqlite3.connect(database,timeout=2,isolation_level=None)) as guard:
        guard.execute('BEGIN IMMEDIATE')
        try:
            with closing(connect_readonly(database.resolve())) as connection:
                counts,fingerprint=inspect_database(connection)
                locations=connection.execute('SELECT b.sha256,b.size_bytes,s.backend_id,s.object_key FROM blobs b LEFT JOIN storage_locations s ON s.blob_sha256=b.sha256 ORDER BY b.sha256,s.backend_id').fetchall()
                object_keys={}
                for digest,size,backend,key in locations:
                    if backend=='local' and key==f'objects/{digest[:2]}/{digest}':object_keys[digest]=(key,size)
                if len(object_keys)!=counts['blobs']:raise RecoveryError('all_blobs_require_local_verified_copy')
                workspace_keys=[]
                for key in regular_files(source_workspace,'jobs'):
                    workspace_keys.append(key)
                    if len(workspace_keys)+len(object_keys)+1>max_files:raise RecoveryError('snapshot_file_budget')
                if len(workspace_keys)+len(object_keys)+1>max_files:raise RecoveryError('snapshot_file_budget')
                for key in workspace_keys:
                    if any(re.search(r'(^\.env($|\.)|\.local\.|credential|token|secret)',part,re.I) for part in key.split('/')):raise RecoveryError('credential_named_workspace_file_refused')
                verify_references(connection,source_workspace,limit=max_bytes)
                inventory={key:digest_file(source_workspace,key,limit=max_bytes) for key in workspace_keys}
                database_bytes=connection.execute('PRAGMA page_count').fetchone()[0]*connection.execute('PRAGMA page_size').fetchone()[0]
                estimate=sum(size for _,size in inventory.values())+sum(size for _,size in object_keys.values())+database_bytes
                if estimate>max_bytes:raise RecoveryError('snapshot_byte_budget')
                if shutil.disk_usage(output_path.parent).free<estimate+64*1024**2:raise RecoveryError('snapshot_disk_capacity')
                output=fresh_root(output_path,(workspace,objects,database.resolve()))
                for name in ('objects','workspace'):
                    (output.root/name).mkdir()
                    (output.root/name/'README.md').write_text('Private snapshot data. Empty directories are valid for a fresh catalogue; no template/model/config files copied.\n',encoding='utf8')
                target=output.root/'database.sqlite3';started=time.monotonic()
                def progress(status,remaining,total):
                    if time.monotonic()-started>300:raise RecoveryError('database_backup_deadline')
                with closing(sqlite3.connect(target)) as copied:connection.backup(copied,pages=128,progress=progress,sleep=.01)
                digest,size=digest_file(output,'database.sqlite3',limit=max_bytes);members=[BackupFile(path='database.sqlite3',sha256=digest,size_bytes=size)];used=size
                with closing(connect_readonly(target)) as copied:
                    if inspect_database(copied)!=(counts,fingerprint):raise RecoveryError('database_snapshot_mismatch')
                for digest,(key,size) in object_keys.items():
                    member=copy_file(source_objects,key,output,'objects/'+key,limit=max_bytes-used);used+=member.size_bytes
                    if member.sha256!=digest or member.size_bytes!=size:raise RecoveryError('source_object_integrity_failed')
                    members.append(member)
                for key,(digest,size) in inventory.items():
                    member=copy_file(source_workspace,key,output,'workspace/'+key,limit=max_bytes-used);used+=member.size_bytes
                    if (member.sha256,member.size_bytes)!=(digest,size):raise RecoveryError('workspace_changed_during_backup')
                    members.append(member)
                if list(regular_files(source_workspace,'jobs'))!=workspace_keys or any(digest_file(source_workspace,key,limit=max_bytes)!=value for key,value in inventory.items()):raise RecoveryError('workspace_changed_during_backup')
                manifest=BackupManifest(created_at=datetime.now(timezone.utc).isoformat(),template_fingerprint=fingerprint,files=tuple(members),table_counts=counts)
                raw=encode(manifest.model_dump(mode='json')).encode()
                if len(raw)>32*1024**2:raise RecoveryError('manifest_byte_budget')
                temporary=output.root/'manifest.partial.json'
                with temporary.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                temporary.rename(output.root/'manifest.json')
                return {'manifest_sha256':sha256(raw).hexdigest(),'files':len(members),'size_bytes':used,'database_integrity':'ok','offline_backup':True}
        finally:guard.rollback()


def verify_backup(source,manifest_sha256,*,max_bytes=50*1024**3):
    if re.fullmatch('[0-9a-f]{64}',manifest_sha256) is None:raise RecoveryError('pinned_manifest_hash_required')
    backend=FilesystemBackend(checked_root(source))
    with backend.open_read('manifest.json') as stream:raw=stream.read(32*1024**2+1)
    if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=manifest_sha256:raise RecoveryError('backup_manifest_hash_mismatch')
    manifest=BackupManifest.model_validate_json(raw)
    if sum(f.size_bytes for f in manifest.files)>max_bytes:raise RecoveryError('snapshot_byte_budget')
    for member in manifest.files:
        if digest_file(backend,member.path,limit=member.size_bytes)!=(member.sha256,member.size_bytes):raise RecoveryError('backup_member_integrity_failed')
    with closing(connect_readonly(backend.root/'database.sqlite3')) as connection:
        if inspect_database(connection)!=(manifest.table_counts,manifest.template_fingerprint):raise RecoveryError('database_manifest_mismatch')
        verify_references(connection,FilesystemBackend(backend.root/'workspace'),limit=max_bytes)
        verify_objects(connection,FilesystemBackend(backend.root/'objects'),limit=max_bytes)
    return backend,manifest


def restore(source,destination,manifest_sha256,*,max_bytes=50*1024**3):
    backend,manifest=verify_backup(source,manifest_sha256,max_bytes=max_bytes)
    target=checked_root(destination,exists=False)
    if shutil.disk_usage(target.parent).free<sum(f.size_bytes for f in manifest.files)+64*1024**2:raise RecoveryError('restore_disk_capacity')
    output=fresh_root(destination,(backend.root,));used=0
    for member in manifest.files:
        copied=copy_file(backend,member.path,output,member.path,limit=member.size_bytes);used+=copied.size_bytes
        if copied!=member:raise RecoveryError('restored_member_integrity_failed')
    for key in ('objects','workspace'):
        path=output.root/key;path.mkdir(exist_ok=True)
        (path/'README.md').write_text('Restored private media-tag data; original source files/templates/models/configuration not copied.\n',encoding='utf8')
    with closing(connect_readonly(output.root/'database.sqlite3')) as connection:
        if inspect_database(connection)!=(manifest.table_counts,manifest.template_fingerprint):raise RecoveryError('restored_database_mismatch')
    receipt={'schema_version':'d07-restore-v1','backup_manifest_sha256':manifest_sha256,'files':len(manifest.files),'size_bytes':used,
        'database_integrity':'ok','fresh_isolated_root':True,'configuration_restored':False,'service_started':False,'business_acceptance':False}
    (output.root/'restore.json').write_text(encode(receipt),encoding='utf8')
    return receipt
