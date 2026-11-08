"""Small, bounded CSV/XLSX value-table imports. Never execute formulas or extract ZIP paths."""
import csv
from hashlib import sha256
from io import BytesIO,StringIO
import json
import posixpath
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile
from pydantic import ValidationError
from media_tag.catalog import AccessDenied,NotFound,RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.storage import IntegrityError
from media_tag.terminology.contracts import Alias
from media_tag.terminology.contracts import ImportRows
from media_tag.workers.evidence import _directory

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def xml(raw):
    text=raw.decode('utf-8-sig')
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():raise ValueError('XML declarations are forbidden')
    return ET.fromstring(text)


def cell_text(element):
    if element is None:return ''
    return ''.join((child.text or '') if child.tag=='{'+NS['s']+'}t' else ''.join(t.text or '' for t in child.findall('s:t',NS))
                   for child in element if child.tag in ('{'+NS['s']+'}t','{'+NS['s']+'}r'))


def table(raw,kind,sheet=None):
    if len(raw)>4*1024**2:raise ValueError('import byte budget')
    if kind=='csv':
        if sheet is not None:raise ValueError('CSV has no worksheet selection')
        try:rows=list(csv.reader(StringIO(raw.decode('utf-8-sig'),newline=''),strict=True))
        except (UnicodeError,csv.Error):raise ValueError('UTF8 CSV required') from None
        sheets=[]
        if len(rows)>1001 or any(len(row)>50 for row in rows):raise ValueError('table budget')
        numbered=[(i,row,[]) for i,row in enumerate(rows,1)]
    elif kind=='xlsx':
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                infos=archive.infolist()
                if len(infos)>128 or sum(i.file_size for i in infos)>16*1024**2 or any(i.flag_bits&1 for i in infos):raise ValueError('XLSX expanded budget')
                if len({i.filename for i in infos})!=len(infos):raise ValueError('duplicate ZIP entries')
                workbook=xml(archive.read('xl/workbook.xml'));relationships=xml(archive.read('xl/_rels/workbook.xml.rels'))
                rels={r.attrib['Id']:r.attrib for r in relationships}
                records=workbook.findall('s:sheets/s:sheet',NS);sheets=[r.attrib['name'] for r in records]
                if len(sheets)!=len(set(sheets)):raise ValueError('duplicate worksheet names')
                if sheet is None:return {'sheets':sheets,'headers':[],'rows':[],'selection_required':True}
                record=next((r for r in records if r.attrib['name']==sheet),None)
                if record is None:raise ValueError('worksheet unavailable')
                relationship=rels[record.attrib['{'+REL+'}id']]
                if relationship.get('TargetMode')=='External':raise ValueError('external worksheet is forbidden')
                target=relationship['Target'];path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                if not path.startswith('xl/worksheets/') or '..' in path.split('/'):raise ValueError('worksheet path escape')
                strings=[]
                if 'xl/sharedStrings.xml' in archive.namelist():
                    strings=[cell_text(si) for si in xml(archive.read('xl/sharedStrings.xml')).findall('s:si',NS)]
                worksheet=xml(archive.read(path));numbered=[]
                for row in worksheet.findall('s:sheetData/s:row',NS):
                    number=int(row.attrib['r']);values={};errors=[]
                    if not 1<=number<=10001:raise ValueError('worksheet row index budget')
                    for cell in row.findall('s:c',NS):
                        address=cell.attrib.get('r','');letters=''.join(c for c in address if c.isalpha());column=0
                        for letter in letters:
                            if not 'A'<=letter<='Z':raise ValueError('invalid cell address')
                            column=column*26+ord(letter)-64
                        if not 1<=column<=50 or column in values:raise ValueError('column budget or duplicate cell')
                        if cell.find('s:f',NS) is not None:errors.append('formula_cell');value=''
                        elif cell.attrib.get('t')=='inlineStr':value=cell_text(cell.find('s:is',NS))
                        else:
                            node=cell.find('s:v',NS);value=node.text or '' if node is not None else ''
                            if cell.attrib.get('t')=='s':value=strings[int(value)]
                            if cell.attrib.get('t')=='b':
                                if value not in ('0','1'):errors.append('invalid_boolean_cell')
                                value='true' if value=='1' else 'false'
                            if cell.attrib.get('t')=='e':errors.append('excel_error_cell')
                        values[column]=value
                    numbered.append((number,[values.get(i,'') for i in range(1,max(values,default=0)+1)],errors))
                if len(numbered)>1001 or len({n for n,_,_ in numbered})!=len(numbered):raise ValueError('row budget or duplicate rows')
                numbered.sort(key=lambda r:r[0])
        except (zipfile.BadZipFile,KeyError,IndexError,ET.ParseError,TypeError,UnicodeError):raise ValueError('invalid XLSX value table') from None
    else:raise ValueError('CSV/XLSX only')
    if not numbered or numbered[0][0]!=1 or numbered[0][2]:raise ValueError('header must be the first row with plain values')
    headers=numbered[0][1]
    if not headers or any(not h.strip() for h in headers) or len(headers)!=len(set(headers)):raise ValueError('explicit unique headers required')
    rows=[]
    for number,values,errors in numbered[1:]:
        if len(values)>len(headers) or any(len(v)>8000 for v in values):errors=errors+['cell_budget_or_extra_columns']
        rows.append({'row':number,'values':dict(zip(headers,values+['']*max(0,len(headers)-len(values)))),'errors':errors})
    return {'sheets':sheets,'sheet':sheet,'headers':headers,'rows':rows,'selection_required':False}


class TermImports:
    def __init__(self,search):self.search=search;self.terms=search.terminology

    def upload(self,raw,kind,*,principal):
        self.terms._manager(principal)
        parsed=table(raw,kind)
        iid='import-'+uuid4().hex;folder=_directory(self.search.router.workspace,'jobs/'+iid)
        (folder/'README.md').write_text('Private user-provided terminology import. Candidate data only; no automatic release.\n',encoding='utf-8')
        path=folder/('source.'+kind)
        with path.open('xb') as stream:stream.write(raw);stream.flush();__import__('os').fsync(stream.fileno())
        digest=sha256(raw).hexdigest();ref=path.relative_to(self.search.router.workspace).as_posix()
        with self.search.database.transaction(write=True) as c:
            c.execute('INSERT INTO terminology_imports VALUES(?,?,?,?,?,?)',(iid,principal.actor_id,kind,digest,ref,utc_now()))
        return {'import_id':iid,'sha256':digest,'format':kind,'sheets':parsed['sheets'],'headers':parsed['headers'],
                'row_count':len(parsed['rows']),'selection_required':parsed['selection_required']}

    def inspect(self,iid,*,principal,sheet=None):
        self.terms._manager(principal)
        with self.search.database.transaction() as c:
            row=c.execute('SELECT * FROM terminology_imports WHERE import_id=?',(iid,)).fetchone()
            if not row:raise NotFound('import unavailable')
            if row['owner_id']!=principal.actor_id:raise AccessDenied('import owner required')
        with self.search.router.files.open_read(row['artifact_ref']) as stream:raw=stream.read(4*1024**2+1)
        if len(raw)>4*1024**2 or sha256(raw).hexdigest()!=row['source_sha256']:raise IntegrityError('import source changed')
        return table(raw,row['format'],sheet)|{'import_id':iid,'sha256':row['source_sha256']}

    def preview(self,iid,spec,*,principal):
        self.terms._manager(principal)
        parsed=self.inspect(iid,principal=principal,sheet=spec.sheet)
        if parsed['selection_required']:raise ValueError('explicit worksheet selection required')
        allowed={'expression','target_id','target_name','relation','context_json','rationale','source_locator','source_quote','source_usage','source_reference','default_expand','ambiguity_group'}
        if not set(spec.columns)<=allowed or not set(spec.defaults)<={'relation','rationale','source_usage'} or 'expression' not in spec.columns:
            raise ValueError('explicit supported column mapping required')
        if not set(spec.columns.values())<=set(parsed['headers']) or len(set(spec.columns.values()))!=len(spec.columns):raise ValueError('unknown or repeated column mapping')
        candidate=self.terms.get(spec.candidate_id,principal=principal)
        if candidate['kind']!='lexicon' or candidate['revision']!=spec.expected_revision or candidate['state']=='published':raise RevisionConflict('current lexicon draft required')
        concepts=candidate['payload']['concepts'];reports=[];converted={}
        for record in parsed['rows']:
            number=record['row'];values={key:record['values'][column] for key,column in spec.columns.items()}
            for key,value in spec.defaults.items():values.setdefault(key,value)
            warnings=[]
            if record['errors']:reports.append({'row':number,'status':'invalid','codes':record['errors']});continue
            if not any(record['values'].values()):reports.append({'row':number,'status':'empty','codes':['empty_row']});continue
            target=values.get('target_id') or None
            if not target and values.get('target_name'):
                targets=[c['entry_id'] for c in concepts if c['name']==values['target_name'] and c['status']=='active']
                if len(targets)==1:target=targets[0]
                else:warnings.append('ambiguous_target_name' if targets else 'unknown_target_name')
            if not target:warnings.append('unknown_target')
            sources=[]
            if values.get('source_usage'):
                sources=[{'kind':'human','locator':values.get('source_locator') or f"import:{iid}:sha256:{parsed['sha256']}:sheet:{spec.sheet or 'CSV'}:row:{number}",
                    'quote':values.get('source_quote') or values.get('expression',''),'usage':values['source_usage'],'reference':values.get('source_reference') or None}]
            else:warnings.append('missing_source_usage')
            try:
                flag=values.get('default_expand','false').lower()
                if flag not in ('true','false',''):raise ValueError('explicit boolean required')
                entry=Alias.model_validate_json(encode({'entry_id':'import-alias-'+iid[7:]+'-'+str(number),'sources':sources,
                    'rationale':values.get('rationale') or spec.note,'expression':values.get('expression',''),'target_id':target,
                    'relation':values.get('relation',''),'context':json.loads(values.get('context_json') or '{}'),
                    'default_expand':flag=='true','ambiguity_group':values.get('ambiguity_group') or None}))
                converted[number]=entry.model_dump(mode='json');reports.append({'row':number,'status':'candidate','codes':warnings,'entry_id':entry.entry_id})
            except (ValueError,ValidationError):reports.append({'row':number,'status':'invalid','codes':warnings+['invalid_alias_contract']})
        selected=list(spec.selected_rows)
        if len(set(selected))!=len(selected) or not set(selected)<={r['row'] for r in reports}:raise ValueError('explicit valid row numbers required')
        if spec.apply and (not selected or not set(selected)<=converted.keys()):raise ValueError('select nonempty rows without format errors')
        scope=selected if selected else sorted(converted)
        result=None
        if scope:
            result=self.terms.import_rows(spec.candidate_id,ImportRows(request_id=spec.request_id,expected_revision=spec.expected_revision,entity='aliases',
                rows=tuple(converted[n] for n in scope),apply=spec.apply,note=spec.note),principal=principal)
        return {'import_id':iid,'sha256':parsed['sha256'],'reports':reports,'selected_rows':scope,'candidate_result':result,
            'applied':bool(result and result['applied']),'source_unchanged':True}
