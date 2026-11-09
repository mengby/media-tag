from typing import Annotated
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel

FIELDS=('画面主体','表现形式','中文检索词','英文检索词')
# Runtime also compares this exact tuple with the original, verified template.
FORMS=('3D动画','2D插图','剖面','对比图','实拍','图标')
from .prompt import PROMPT_VERSION
PREPROCESS='visual-fields-frame-v1:'+PROMPT_VERSION


class SemanticDeployment(ContractModel):
    enabled: bool=False
    model_root: str='cache/models/semantic'


class SemanticOptions(ContractModel):
    max_pixels: Annotated[int,Field(ge=128**2,le=768**2)]=512**2
    max_new_tokens: Annotated[int,Field(ge=128,le=384)]=256


class Caption(ContractModel):
    subject: Annotated[str,Field(max_length=120)]
    form: Annotated[str,Field(max_length=32)]
    keywords_zh: Annotated[tuple[Annotated[str,Field(min_length=1,max_length=48)],...],Field(max_length=8)]
    keywords_en: Annotated[tuple[Annotated[str,Field(min_length=1,max_length=64)],...],Field(max_length=8)]
    uncertainties: Annotated[tuple[Annotated[str,Field(min_length=1,max_length=200)],...],Field(max_length=4)]

    @model_validator(mode='after')
    def check(self):
        import re
        if self.form and self.form not in FORMS:raise ValueError('form outside original template')
        for values in (self.keywords_zh,self.keywords_en):
            if len(set(values))!=len(values) or any(any(c in v for c in ('\x00','\n','\r',',','，',';','；','://')) for v in values):raise ValueError('invalid keyword list')
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 -]*',v) or not re.search('[A-Za-z]',v) for v in self.keywords_en):raise ValueError('English keywords must be simple phrases')
        if any(c in self.subject for c in ('\x00','\n','\r','://')):raise ValueError('invalid subject')
        if any(v!=v.strip() for v in (self.subject,self.form,*self.keywords_zh,*self.keywords_en)):raise ValueError('untrimmed value')
        return self


def parse_caption(text):
    """Conservative format repair; invalid options become unknown, never new enums."""
    import json,re
    value=json.loads(text);changes=[]
    if not isinstance(value,dict) or set(value)!=set(Caption.model_fields):raise ValueError('exact caption keys required')
    for field in ('subject','form'):
        if not isinstance(value[field],str):raise ValueError('text field required')
        value[field]=value[field].strip()
    if value['form'] and value['form'] not in FORMS:
        changes.append('form_outside_template_left_unknown');value['form']=''
    if len(value['subject'])>120 or any(c in value['subject'] for c in ('\x00','\n','\r','://')):
        changes.append('invalid_subject_left_unknown');value['subject']=''
    for key in ('keywords_zh','keywords_en'):
        if not isinstance(value[key],list):raise ValueError('keyword array required')
        kept=[]
        for raw in value[key]:
            if not isinstance(raw,str):changes.append(key+'_invalid_item_omitted');continue
            word=raw.strip();limit=48 if key=='keywords_zh' else 64
            valid=bool(word and len(word)<=limit and not any(c in word for c in ('\x00','\n','\r',',','，',';','；','://')))
            if key=='keywords_en':valid=valid and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 -]*',word) and re.search('[A-Za-z]',word))
            if valid and word not in kept and len(kept)<8:kept.append(word)
            else:changes.append(key+'_invalid_duplicate_or_excess_omitted')
        value[key]=kept
    if not isinstance(value['uncertainties'],list) or any(not isinstance(v,str) for v in value['uncertainties']):raise ValueError('uncertainty array required')
    value['uncertainties']=[v.strip() for v in value['uncertainties'] if v.strip() and len(v.strip())<=200][:4]
    return Caption.model_validate_json(json.dumps(value)),sorted(set(changes))
