from io import BytesIO
import csv
import json
import zipfile
from xml.sax.saxutils import escape
import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.search.imports import table
from media_tag.search.models import ImportMapping
from terminology.lexicon.conftest import create


def xlsx(rows,*,formula=False):
    target=BytesIO()
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="候选表" sheetId="1" r:id="s1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="s1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>')
        xml=['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        for number,row in enumerate(rows,1):
            xml.append(f'<row r="{number}">')
            for col,value in enumerate(row):
                address=chr(65+col)+str(number)
                xml.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
            if formula and number==2:xml.append(f'<c r="E{number}"><f>HYPERLINK("https://invalid.test")</f><v>1</v></c>')
            xml.append('</row>')
        archive.writestr('xl/worksheets/sheet1.xml',''.join(xml)+'</sheetData></worksheet>')
    return target.getvalue()


def test_csv_scope_mapping_invalid_rows_never_silently_apply(sought):
    env=sought.env;imports=sought.search.imports
    raw='说法,专业ID,关系\n新增圆圈,concept-a,equivalent\n无来源说法,,related\n坏关系,concept-a,not-real\n'.encode('utf-8-sig')
    uploaded=imports.upload(raw,'csv',principal=env.manager)
    candidate=create(env,sought.payload,base=sought.lex['release_id'],actor=env.manager)
    spec=ImportMapping(request_id='preview',candidate_id=candidate['candidate_id'],expected_revision=1,columns={'expression':'说法','target_id':'专业ID','relation':'关系'},
        defaults={'source_usage':'Only this synthetic engineering candidate review'},note='Synthetic explicit import')
    preview=imports.preview(uploaded['import_id'],spec,principal=env.manager)
    assert [r['status'] for r in preview['reports']]==['candidate','candidate','invalid']
    assert not preview['applied']
    with pytest.raises(ValueError):imports.preview(uploaded['import_id'],spec.model_copy(update={'request_id':'all','selected_rows':(2,4),'apply':True}),principal=env.manager)
    applied=imports.preview(uploaded['import_id'],spec.model_copy(update={'request_id':'apply','selected_rows':(2,),'apply':True}),principal=env.manager)
    assert applied['applied'] and applied['candidate_result']['revision']==2
    with pytest.raises(RevisionConflict):imports.preview(uploaded['import_id'],spec.model_copy(update={'request_id':'stale'}),principal=env.manager)
    with pytest.raises(AccessDenied):imports.inspect(uploaded['import_id'],principal=env.owner)
    assert imports.inspect(uploaded['import_id'],principal=env.manager)['sha256']==uploaded['sha256']


def test_xlsx_requires_sheet_and_rejects_formula_rows_without_executing_them(sought):
    env=sought.env;imports=sought.search.imports
    raw=xlsx([['说法','专业名称','关系','说明'],['新说法','合成圆形','equivalent','source']],formula=True)
    uploaded=imports.upload(raw,'xlsx',principal=env.manager)
    assert uploaded['selection_required'] and uploaded['sheets']==['候选表']
    parsed=imports.inspect(uploaded['import_id'],principal=env.manager,sheet='候选表')
    assert 'formula_cell' in parsed['rows'][0]['errors']
    candidate=create(env,sought.payload,base=sought.lex['release_id'],actor=env.manager)
    spec=ImportMapping(request_id='xlsx-preview',candidate_id=candidate['candidate_id'],expected_revision=1,sheet='候选表',columns={'expression':'说法','target_name':'专业名称','relation':'关系'},note='Synthetic formula preflight')
    result=imports.preview(uploaded['import_id'],spec,principal=env.manager)
    assert result['reports'][0]['status']=='invalid' and not result['applied']


@pytest.mark.parametrize('raw,kind',[ (b'a,a\nx,y','csv'),(b'\xff','csv'),(b'not-a-zip','xlsx')])
def test_bad_format_is_explicit(raw,kind):
    with pytest.raises(ValueError):table(raw,kind)


def test_xlsx_xml_entities_and_path_escape_are_rejected():
    raw=xlsx([['a'],['b']]);source=zipfile.ZipFile(BytesIO(raw));target=BytesIO()
    with zipfile.ZipFile(target,'w') as archive:
        for item in source.infolist():
            data=source.read(item.filename)
            if item.filename=='xl/_rels/workbook.xml.rels':data=data.replace(b'worksheets/sheet1.xml',b'../../outside.xml')
            archive.writestr(item.filename,data)
    with pytest.raises(ValueError):table(target.getvalue(),'xlsx','候选表')
    target=BytesIO()
    with zipfile.ZipFile(target,'w') as archive:
        for item in source.infolist():
            data=source.read(item.filename)
            if item.filename=='xl/workbook.xml':data=b'<!DOCTYPE a [<!ENTITY x "unsafe">]>'+data
            archive.writestr(item.filename,data)
    with pytest.raises(ValueError):table(target.getvalue(),'xlsx')
