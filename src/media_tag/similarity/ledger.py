"""Separate private relationship ledger; decisions never mutate catalogue assets/tags."""
from datetime import datetime, timezone
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.catalog import AccessDenied, CatalogError, RevisionConflict
from media_tag.contracts.models import ContractModel, Identifier, PositiveInt
from media_tag.files.service import FileError
from media_tag.storage.filesystem import require_local_path

from .contracts import Candidate, SourceVersion


class RelationDecision(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    relation: Literal['same_recording', 'variant_of', 'related', 'different', 'uncertain']
    direction: Literal['left_to_right', 'right_to_left'] | None = None
    note: Annotated[str, Field(min_length=1, max_length=1000, pattern=r'\S')]

    @model_validator(mode='after')
    def explicit_direction(self):
        if (self.relation == 'variant_of') != (self.direction is not None):
            raise ValueError('only variant_of requires explicit direction')
        return self


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


class RelationLedger:
    def __init__(self, directory: Path, authorization):
        self.root, self.authorization = Path(directory), authorization
        if not self.root.is_absolute():
            raise ValueError('absolute private relation directory required')
        require_local_path(self.root)
        self.root.mkdir(exist_ok=True)
        readme = self.root/'README.md'
        self.path = self.root/'relations.sqlite3'
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS relations(
                relation_id TEXT PRIMARY KEY,left_source TEXT NOT NULL,right_source TEXT NOT NULL,
                candidate_json TEXT NOT NULL,revision INTEGER NOT NULL,decision_json TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS requests(
                actor_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,
                PRIMARY KEY(actor_id,request_id))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS events(
                event_id INTEGER PRIMARY KEY,relation_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                action TEXT NOT NULL,detail_json TEXT NOT NULL,created_at TEXT NOT NULL)''')
        if not readme.exists():
            readme.write_text('Private similarity relationships and decision audit. Does not merge media, copy labels or approve privacy/medical use.\n', encoding='utf8')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            application=connection.execute('PRAGMA application_id').fetchone()[0]
            version=connection.execute('PRAGMA user_version').fetchone()[0]
            tables=connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if application not in (0,0x4D545352) or application==0 and tables or version not in (0,1):
                raise ValueError('foreign or unsupported relation database')
            if application==0:
                connection.execute('PRAGMA application_id=1297371986')
            if version==0:
                connection.execute('PRAGMA user_version=1')
            connection.execute('PRAGMA journal_mode=WAL')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _human(principal):
        if principal.role not in ('editor', 'admin'):
            raise AccessDenied('human relation editor required')

    def _sources(self, left, right, principal, *, edit=False):
        self.authorization.authorize(left, principal=principal, edit=edit)
        self.authorization.authorize(right, principal=principal, edit=edit)

    @staticmethod
    def _receipt(connection, principal, request_id, fingerprint):
        row = connection.execute('SELECT * FROM requests WHERE actor_id=? AND request_id=?', (principal.actor_id,request_id)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise RevisionConflict('relation request ID reused with different input')
            return json.loads(row['result_json']) | {'reused_request': True}

    @staticmethod
    def _view(row):
        return {'relation_id': row['relation_id'], 'revision': row['revision'],
            'candidate': json.loads(row['candidate_json']), 'decision': json.loads(row['decision_json']) if row['decision_json'] else None,
            'state': 'reviewed_relation' if row['decision_json'] else 'pending_human_relation',
            'medical_confirmation': False, 'privacy_confirmation': False, 'labels_copied': False,
            'ownership_changed': False, 'reused_request': False}

    def offer(self, candidate: Candidate, *, request_id: str, principal):
        self._human(principal)
        # Identifier validation without accepting a caller-provided actor/approval.
        RelationDecision(request_id=request_id, expected_revision=1, relation='uncertain', note='candidate submission')
        if candidate.left.asset_id == candidate.right.asset_id:
            raise ValueError('two distinct asset records required')
        self._sources(candidate.left,candidate.right,principal)
        self.authorization.validate_candidate(candidate,principal=principal)
        payload = candidate.model_dump(mode='json')
        fingerprint = sha256(_json({'action':'offer','candidate':payload}).encode()).hexdigest()
        relation_id = 'relation-'+sha256(_json(payload).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = self._receipt(connection,principal,request_id,fingerprint)
            if previous:
                return previous
            connection.execute('''INSERT OR IGNORE INTO relations VALUES(?,?,?,?,1,NULL,?,?)''',
                (relation_id,_json(payload['left']),_json(payload['right']),_json(payload),now,now))
            row = connection.execute('SELECT * FROM relations WHERE relation_id=?',(relation_id,)).fetchone()
            result = self._view(row)
            connection.execute('INSERT INTO requests VALUES(?,?,?,?)',(principal.actor_id,request_id,fingerprint,_json(result)))
            connection.execute('INSERT INTO events(relation_id,actor_id,action,detail_json,created_at) VALUES(?,?,?,?,?)',
                (relation_id,principal.actor_id,'offer',_json({'candidate_sha256':fingerprint}),now))
            self._sources(candidate.left,candidate.right,principal)
            self.authorization.validate_candidate(candidate,principal=principal)
            return result

    def get(self, relation_id, *, principal):
        if principal.role not in ('reader','editor','admin'):
            raise AccessDenied('human relation reader required')
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM relations WHERE relation_id=?',(relation_id,)).fetchone()
            if row is None:
                raise ValueError('relation unavailable')
            left = SourceVersion.model_validate_json(row['left_source'])
            right = SourceVersion.model_validate_json(row['right_source'])
            self._sources(left,right,principal)
            self.authorization.validate_candidate(Candidate.model_validate_json(row['candidate_json']),principal=principal)
            return self._view(row)

    def list(self, *, principal, limit=100):
        if principal.role not in ('reader','editor','admin'):
            raise AccessDenied('human relation reader required')
        if not 1 <= limit <= 100:
            raise ValueError('relation page budget')
        output=[]
        with self._connect() as connection:
            rows=connection.execute('SELECT relation_id FROM relations ORDER BY updated_at DESC,relation_id LIMIT 1000').fetchall()
        for row in rows:
            try:
                output.append(self.get(row[0],principal=principal))
            except (CatalogError,FileError):
                continue
            if len(output) == limit:
                break
        return output

    def decide(self, relation_id, spec: RelationDecision, *, principal):
        self._human(principal)
        now=datetime.now(timezone.utc).isoformat()
        fingerprint=sha256(_json({'action':'decide','relation_id':relation_id,'spec':spec.model_dump(mode='json')}).encode()).hexdigest()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT * FROM relations WHERE relation_id=?',(relation_id,)).fetchone()
            if row is None:
                raise ValueError('relation unavailable')
            left=SourceVersion.model_validate_json(row['left_source']);right=SourceVersion.model_validate_json(row['right_source'])
            self._sources(left,right,principal,edit=True)
            self.authorization.validate_candidate(Candidate.model_validate_json(row['candidate_json']),principal=principal)
            previous=self._receipt(connection,principal,spec.request_id,fingerprint)
            if previous:
                return previous
            if row['revision'] != spec.expected_revision:
                raise RevisionConflict('relation revision changed')
            decision={'relation':spec.relation,'direction':spec.direction,'note':spec.note,
                'decided_by':principal.actor_id,'decided_at':now,'expected_revision':spec.expected_revision}
            connection.execute('UPDATE relations SET revision=revision+1,decision_json=?,updated_at=? WHERE relation_id=?',(_json(decision),now,relation_id))
            result=self._view(connection.execute('SELECT * FROM relations WHERE relation_id=?',(relation_id,)).fetchone())
            connection.execute('INSERT INTO requests VALUES(?,?,?,?)',(principal.actor_id,spec.request_id,fingerprint,_json(result)))
            connection.execute('INSERT INTO events(relation_id,actor_id,action,detail_json,created_at) VALUES(?,?,?,?,?)',
                (relation_id,principal.actor_id,'decide',_json(decision),now))
            self._sources(left,right,principal,edit=True)
            self.authorization.validate_candidate(Candidate.model_validate_json(row['candidate_json']),principal=principal)
            return result

    def history(self, relation_id, *, principal):
        self.get(relation_id,principal=principal)
        with self._connect() as connection:
            rows=connection.execute('SELECT event_id,actor_id,action,detail_json,created_at FROM events WHERE relation_id=? ORDER BY event_id',
                (relation_id,)).fetchall()
        self.get(relation_id,principal=principal)
        return [{'event_id':row['event_id'],'actor_id':row['actor_id'],'action':row['action'],
            'detail':json.loads(row['detail_json']),'created_at':row['created_at']} for row in rows]
