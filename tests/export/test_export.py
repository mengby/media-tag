from hashlib import sha256
from io import StringIO
import csv,json
import pytest
from media_tag.catalog import RevisionConflict,AccessDenied
from media_tag.export.models import ExportSubmission,AssetSelection,GapSelection
from media_tag.export.service import safe_cell
from media_tag.review.policy import ASSETS,GAPS
from review.conftest import current,observation,decide
from review.test_gaps import create as create_gap


def spec(env,request='export',**kwargs):
    asset=current(env)
    return ExportSubmission(request_id=request,assets=(AssetSelection(asset_id=asset.asset_id,expected_revision=asset.revision,source_sha256=asset.sha256),),**kwargs)


def rows(raw):return list(csv.reader(StringIO(raw.decode('utf-8-sig'),newline='')))


def test_actual_two_headers_typed_values_quality_and_stable_authenticated_links(reviewed):
    env=reviewed;obs=observation(env)
    decide(env,'画面主体',values=('=HYPERLINK("evil")\n"quoted",中文',),obs=obs)
    gap=create_gap(env)
    result=env.exports.create(spec(env,gaps=(GapSelection(gap_id=gap['gap_id'],expected_revision=1),)),principal=env.owner)
    assets=env.exports.content(result['export_id'],'assets.csv',principal=env.owner)
    gaps=env.exports.content(result['export_id'],'gaps.csv',principal=env.owner)
    assert assets.startswith(b'\xef\xbb\xbf') and gaps.startswith(b'\xef\xbb\xbf')
    parsed=rows(assets);assert tuple(parsed[0])==env.contract.table(ASSETS).header and len(parsed[1])==35
    assert tuple(rows(gaps)[0])==env.contract.table(GAPS).header and len(rows(gaps)[1])==14
    item=dict(zip(parsed[0],parsed[1]))
    assert item['画面主体'].startswith("'=HYPERLINK") and 'quoted' in item['画面主体']
    assert current(env).effective_values[0].value.values[0].startswith('=HYPERLINK')
    assert item['SHA256']==env.asset.sha256 and item['本地路径']==''
    assert item['下载链接'].startswith('/v1/assets/') and 'sha256=' in item['下载链接'] and 'token' not in item['下载链接']
    assert item['透明底']=='true' and item['有原声']=='false' and item['时长(秒)']==''
    assert item['标签状态']=='AI初稿' and item['许可状态']=='未核实' and not item['打标人']
    quality=json.loads(env.exports.content(result['export_id'],'quality.json',principal=env.owner))
    assert any(i['code']=='formula_escaped' for i in quality['issues'])
    assert any(i['field']=='提出人' and i['code']=='external_person_mapping_missing' for i in quality['issues'])


@pytest.mark.parametrize('value',('=1','+SUM(1)','-5','@danger',' \t=1','\n=1','\ufeff=1','\u200b+1'))
def test_formula_prefix_variants(value):
    cell,changed=safe_cell(value);assert changed and cell=="'"+value


def test_unknown_is_empty_not_false_and_has_distinct_quality_states(reviewed):
    env=reviewed
    # Missing technical facts are not converted to booleans by the export projection.
    original=env.exports._probe;env.exports._probe=lambda *a:{}
    result=env.exports.create(spec(env),principal=env.owner)
    item=dict(zip(*rows(env.exports.content(result['export_id'],'assets.csv',principal=env.owner))))
    assert item['透明底']==item['有原声']==item['时长(秒)']==''
    issues=json.loads(env.exports.content(result['export_id'],'quality.json',principal=env.owner))['issues']
    assert any(i['field']=='有原声' and i['code']=='technical_unknown' for i in issues)
    for state in ('unknown','no_match','not_applicable','failed'):
        decide(env,'画面主体',state=state)
        new=env.exports.create(spec(env,'state-'+state),principal=env.owner)
        row=dict(zip(*rows(env.exports.content(new['export_id'],'assets.csv',principal=env.owner))))
        assert row['画面主体']==''
        report=json.loads(env.exports.content(new['export_id'],'quality.json',principal=env.owner))
        assert any(i['field']=='画面主体' and i['code']==state for i in report['issues'])



def test_failure_preserves_previous_head_and_bytes_then_failed_attempt_retries(reviewed,monkeypatch):
    env=reviewed;old=env.exports.create(spec(env),principal=env.owner);before=env.exports.content(old['export_id'],'assets.csv',principal=env.owner)
    writer=env.exports._write
    def fail(folder,name,raw):
        if name=='gaps.csv':raise OSError('Synthetic disk fault')
        return writer(folder,name,raw)
    monkeypatch.setattr(env.exports,'_write',fail)
    failed=spec(env,'fault',include_ai_drafts=True)
    with pytest.raises(OSError):env.exports.create(failed,principal=env.owner)
    assert env.clients.owner.get('/review/exports/latest').json()['export_id']==old['export_id']
    assert env.exports.content(old['export_id'],'assets.csv',principal=env.owner)==before
    monkeypatch.setattr(env.exports,'_write',writer)
    repaired=env.exports.create(failed,principal=env.owner)
    assert repaired['manifest']['attempt']==2
    assert env.exports.create(failed,principal=env.owner)['reused']


def test_export_download_rechecks_revision_owner_acl_and_published_hash(reviewed):
    env=reviewed;result=env.exports.create(spec(env),principal=env.owner)
    assert env.clients.other.get('/review/exports/'+result['export_id']+'/content/assets.csv').status_code==403
    path=env.workspace/'jobs'/result['export_id']/'attempt-1/assets.csv'
    original=path.read_bytes();path.write_bytes(b'corrupt')
    assert env.clients.owner.get('/review/exports/'+result['export_id']+'/content/assets.csv').status_code==422
    regenerated=env.exports.create(spec(env,'regenerate-corrupt-cache'),principal=env.owner)
    assert regenerated['export_id']!=result['export_id']
    assert env.exports.content(regenerated['export_id'],'assets.csv',principal=env.owner)==original
    path.write_bytes(original)
    decide(env,'画面主体',values=('Synthetic changed value',))
    assert env.clients.owner.get('/review/exports/'+result['export_id']+'/content/assets.csv').status_code==409


def test_export_requires_explicit_unique_selection(reviewed):
    env=reviewed
    with pytest.raises(ValueError):ExportSubmission(request_id='empty')
    selection=spec(env).assets[0]
    with pytest.raises(ValueError):ExportSubmission(request_id='duplicate',assets=(selection,selection))
    with pytest.raises(AccessDenied):env.exports.create(spec(env),principal=env.other)


def test_opt_in_ai_drafts_are_separate_and_never_replace_explicit_human_unknown(reviewed):
    from review.test_suggestions import machine_suggestion
    env=reviewed;accepted,_,_=machine_suggestion(env,field='用途大类',value='信息图解')
    assert accepted['accepted']
    one=env.exports.create(spec(env),principal=env.owner)
    assert dict(zip(*rows(env.exports.content(one['export_id'],'assets.csv',principal=env.owner))))['用途大类']==''
    draft=env.exports.create(spec(env,'include-ai',include_ai_drafts=True),principal=env.owner)
    item=dict(zip(*rows(env.exports.content(draft['export_id'],'assets.csv',principal=env.owner))))
    assert item['用途大类']=='信息图解' and item['标签状态']=='AI初稿'
    decide(env,'用途大类',state='unknown')
    manual=env.exports.create(spec(env,'respect-human',include_ai_drafts=True),principal=env.owner)
    assert dict(zip(*rows(env.exports.content(manual['export_id'],'assets.csv',principal=env.owner))))['用途大类']==''
