"""D17 stable, read-only fixed-version retrieval over the same personnel search policy."""
from typing import Annotated,Literal
from pydantic import Field
from media_tag.catalog import AccessDenied,NotFound,RevisionConflict
from media_tag.contracts.models import ContractModel,Identifier
from media_tag.files.service import FileError
from media_tag.terminology.contracts import Context
from media_tag.terminology.lexicon.service import fingerprint
from media_tag.terminology.resolve.models import ResolveInput,QueryInputError
from .models import SearchRequest,AssetFilters

SCHEMA='d17-search-v1'


class AssetSearch(ContractModel):
    schema_version: Literal['d17-search-v1']=SCHEMA
    query: Annotated[str,Field(min_length=1,max_length=2000,pattern=r'\S')]
    shot_intent: Annotated[str,Field(max_length=2000)]=''
    context: Context=Context()
    filters: AssetFilters=AssetFilters()
    selected_concept_ids: Annotated[tuple[Identifier,...],Field(max_length=20)]=()
    selection_lexicon_release_id: Identifier | None=None
    include_drafts: bool=False
    include_visual_drafts: bool=False
    literal_only: bool=False
    use_model: bool=True
    limit: Annotated[int,Field(strict=True,ge=1,le=50)]=10
    cursor: Annotated[str,Field(max_length=2048)]=''


class AssetSearchService:
    def __init__(self,search):self.search=search;self.files=search.files

    def query(self,spec,*,principal):
        request=SearchRequest(request=ResolveInput(query=spec.query,context=spec.context,use_model=spec.use_model),
            filters=spec.filters,selected_concept_ids=spec.selected_concept_ids,selection_lexicon_release_id=spec.selection_lexicon_release_id,
            literal_only=spec.literal_only,include_drafts=spec.include_drafts,include_visual_drafts=spec.include_visual_drafts,prefer_confirmed=True,limit=spec.limit,cursor=spec.cursor,
            request_context_sha256=fingerprint(spec.model_dump(mode='json',exclude={'cursor'})))
        try:
            result=self.search.search(request,principal=principal)
            items=[]
            for group in result['items']:
                selected=group['versions'][-1];detail=self.files.details(selected['asset_id'],principal=principal)
                if detail['revision']!=selected['revision'] or detail['sha256']!=selected['sha256'] or detail['file_access']!='authorized':
                    raise FileError(409,'search_results_changed',retryable=True)
                fragments=[];basis=detail['technical']['time_basis'];duration=basis['duration_seconds']
                for reason in selected['reasons']:
                    if reason['kind']!='human_concept' or reason.get('start_seconds') is None:continue
                    start,end=reason['start_seconds'],reason['end_seconds']
                    if duration is None or basis['normalization_origin_seconds'] is None or not 0<=start<end<=duration:continue
                    fragments.append({'asset_id':detail['asset_id'],'sha256':detail['sha256'],'start_seconds':start,'end_seconds':end,
                        'interval':'half_open','time_basis':basis,'concept_id':reason['concept_id'],'link_id':reason['link_id'],
                        'polarity':reason['polarity'],'direction':reason['direction'],'evidence':'human_continuous_observation',
                        'current_use_review':'not_checked'})
                items.append({key:detail[key] for key in ('asset_id','root_asset_id','revision','sha256','size_bytes','name','kind','technical','transform','sources','effective_values','index_status')}|
                    {'ranking_score':selected['ranking_score'],'match_reasons':selected['reasons'],'fragments':fragments,
                     'download_manifest':detail['download_manifest'],'preview_manifest':self.files._entry(detail['asset_id'],detail['sha256'],'preview-manifest'),
                     'file_access':'authorized','byte_integrity':'verify_on_manifest_and_download','license_status':'unverified',
                     'privacy_gate':'passed_for_this_request','current_use_review':'not_checked','medical_use_approved':False})
            return {'schema_version':SCHEMA,'state':'degraded' if result['resolution']['state']=='degraded' else 'ready',
                'result_state':'matches' if items else 'empty','items':items,'next_cursor':result['next'],'limit':spec.limit,
                'resolution':result['resolution'],'selected_concept_ids':result['selected_concept_ids'],'intent':result['intent'],
                'empty_reason':result['empty_reason'],'search_versions':result['resolution'].get('versions'),'index_revision':result['index_revision'],
                'ranking_algorithm':'d17-confirmed-first-v1','score_kind':'relative_priority_not_medical_probability',
                'filter_unknown_policy':'exclude_if_requested','include_drafts':spec.include_drafts,
                'shot_intent':spec.shot_intent,'shot_intent_application':'informational; structured query/context/filters determine matching',
                'whole_file_download':True,'automatic_selection':False,'current_use_review':'not_checked','visual_drafts':result['visual_drafts']}
        except AccessDenied:raise FileError(403,'search_access_denied') from None
        except NotFound:raise FileError(404,'search_resource_unavailable') from None
        except RevisionConflict:raise FileError(409,'search_results_changed',retryable=True) from None
        except QueryInputError:raise FileError(422,'invalid_search_request') from None
        except ValueError:raise FileError(503,'search_metadata_unavailable',retryable=False) from None
