"""Conservative contract interpretation and an explicitly pinned taxonomy consumer, not a publisher."""
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from media_tag.catalog.models import SourceInput
from media_tag.catalog.service import encode
from media_tag.contracts.templates import read_local_file
from media_tag.contracts.vocabulary import load_published_vocabulary, parse_json, VocabularyManifest

ASSETS = 'feishu_asset_index_template.csv'
GAPS = 'feishu_gap_table_template.csv'
MEDICAL = frozenset(('科室','器官/系统','疾病/症状','过程/状态'))
CONTROLLED = frozenset(('标签状态','打标人','素材ID'))


class ReviewPolicy:
    def __init__(self, contract, config):
        self.contract, self.config = contract, config
        self.asset_fields = dict((d.name,d) for d in contract.field_definitions[0][1])
        self.gap_fields = dict((d.name,d) for d in contract.fields_for(GAPS)) if GAPS in dict(contract.field_definitions) else {}
        self.taxonomy = {}
        pinned = None
        if config.taxonomy_manifest:
            path = Path(config.taxonomy_manifest)
            from media_tag.storage.filesystem import require_local_path
            require_local_path(path)
            if not path.is_absolute() or not config.taxonomy_version or not config.taxonomy_manifest_sha256:
                raise ValueError('taxonomy consumer requires absolute manifest and explicit version/hash')
            raw = read_local_file(path.parent,path.name)
            if sha256(raw).hexdigest()!=config.taxonomy_manifest_sha256: raise ValueError('taxonomy manifest hash mismatch')
            manifest = load_published_vocabulary(path,expected_kind='taxonomy',expected_version=config.taxonomy_version)
            pinned_manifest=VocabularyManifest.model_validate_json(json.dumps(parse_json(raw)))
            if manifest!=pinned_manifest:raise ValueError('pinned taxonomy manifest changed while loading')
            if manifest.published_by not in config.trusted_publishers: raise ValueError('untrusted taxonomy publisher')
            content_raw=read_local_file(path.parent,manifest.content_file)
            if sha256(content_raw).hexdigest()!=manifest.content_sha256:raise ValueError('taxonomy content changed while loading')
            content = parse_json(content_raw)
            if isinstance(content,dict) and content.get('schema_version')=='d13-taxonomy-v1':
                from media_tag.terminology.contracts import Taxonomy
                from media_tag.terminology.lexicon.validation import issues,option_fields
                projected=content.get('fields')
                taxonomy=Taxonomy.model_validate_json(json.dumps({k:v for k,v in content.items() if k!='fields'}))
                if issues(taxonomy,contract) or projected!=option_fields(taxonomy):
                    raise ValueError('invalid D13 taxonomy or projection')
                content={'schema_version':'d06-taxonomy-options-v1','template_fingerprint':taxonomy.template_fingerprint,'fields':projected}
            if set(content)!={'schema_version','template_fingerprint','fields'} or content['schema_version']!='d06-taxonomy-options-v1' or content['template_fingerprint']!=contract.fingerprint:
                raise ValueError('taxonomy consumer contract mismatch')
            if not isinstance(content['fields'],dict): raise ValueError('taxonomy fields must be explicit')
            for field,values in content['fields'].items():
                definition=self.asset_fields.get(field)
                if not definition or definition.selection_policy!='external_vocabulary' or not isinstance(values,list) or not values:
                    raise ValueError('only explicit external taxonomy options are accepted')
                if any(not isinstance(v,str) or not v.strip() or len(v)>200 or any(mark in v for mark in ('…','...','|',';')) for v in values):
                    raise ValueError('taxonomy contains placeholder or ambiguous options')
                if len(set(values))!=len(values):raise ValueError('duplicate taxonomy options')
                self.taxonomy[field]=tuple(values)
            pinned = manifest.content_sha256
        elif config.taxonomy_version or config.taxonomy_manifest_sha256:
            raise ValueError('incomplete taxonomy binding')
        self.digest=sha256(encode({'version':'d06-review-v1','contract':contract.fingerprint,'taxonomy':pinned,
                                  'manifest':config.taxonomy_manifest_sha256}).encode()).hexdigest()

    def options(self, definition):
        if definition.selection_policy=='shared_asset_options':
            definition=self.asset_fields[definition.name]
        if definition.selection_policy=='external_vocabulary':
            return self.taxonomy.get(definition.name)
        if definition.field_type not in ('单选','多选'): return None
        if definition.selection_policy=='dynamic_ids': return ()
        text=definition.options_format
        # The local contract's terminal applicability note is not a selection label.
        text=re.sub(r'（[^（）]*用）$','',text)
        if '|' not in text: return None
        values=tuple(text.split('|'))
        if any(not v or any(mark in v for mark in ('…','...','锁定列表')) for v in values):return None
        return values

    def validate(self, definition, value, *, gap=False):
        if value.state!='observed':return
        values=value.values
        if any(len(v)>8000 or '\x00' in v for v in values):raise ValueError('field value budget exceeded')
        kind=definition.field_type
        if kind not in ('多选','关联') and len(values)!=1:raise ValueError('scalar field requires one value')
        if kind in ('单选','多选'):
            options=self.options(definition)
            if options is None:raise ValueError('published selection options unavailable')
            if not set(values)<=set(options):raise ValueError('value is outside published selection options')
        elif kind=='数字':
            from decimal import Decimal, InvalidOperation
            try:number=Decimal(values[0])
            except InvalidOperation:raise ValueError('numeric value required') from None
            if not number.is_finite() or number<0:raise ValueError('finite nonnegative number required')
        elif kind=='复选框' and values[0] not in ('true','false'):
            raise ValueError('explicit boolean required')
        elif kind=='日期':
            if date.fromisoformat(values[0]).isoformat()!=values[0]:raise ValueError('ISO date required')
        elif kind=='超链接':SourceInput(reference=values[0])
        elif kind in ('附件','人员'):
            raise ValueError('typed external identity/attachment requires dedicated mapping')

    def validate_suggestion(self, definition, suggestion, result):
        self.validate(definition,suggestion.value)
        if suggestion.field=='含人脸' and suggestion.value.state=='observed' and '已处理副本' in suggestion.value.values:
            raise ValueError('models cannot claim approved processed copies')
        if suggestion.field in MEDICAL and suggestion.value.state=='observed':
            evidence=[e for e in result.evidence if e.evidence_id in suggestion.evidence_ids]
            if not evidence or any(e.kind in ('filename','human_record') for e in evidence):
                raise ValueError('medical suggestions require actual visual/audio evidence')
            if suggestion.field=='过程/状态' and any('→' in v or '->' in v for v in suggestion.value.values) and not any(e.kind=='video_segment' for e in evidence):
                raise ValueError('process candidates require continuous video evidence')


def basis(connection,asset,policy_digest):
    values=[json.loads(r[0]) for r in connection.execute('SELECT payload FROM effective_values WHERE asset_id=? ORDER BY field',(asset['asset_id'],))]
    return sha256(encode({'source':asset['blob_sha256'],'name':asset['name'],'values':values,'policy':policy_digest}).encode()).hexdigest()


def index_status(connection,asset,policy=None):
    row=connection.execute('SELECT * FROM index_confirmations WHERE asset_id=? ORDER BY confirmation_id DESC LIMIT 1',(asset['asset_id'],)).fetchone()
    if not row:return 'ai_draft'
    if row['decision']=='confirm' and policy and row['policy_sha256']==policy.digest and row['basis_sha256']==basis(connection,asset,policy.digest):
        return 'confirmed'
    return 'needs_review'
