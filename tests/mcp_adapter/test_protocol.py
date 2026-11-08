import asyncio
import json
import pytest
pytest.importorskip('mcp')
from mcp import Client
from media_tag.mcp.server import build_server,MODELS
from .support import facade


@pytest.mark.parametrize('mode',['2026-07-28','legacy'])
def test_actual_sdk_discovers_only_read_tools_and_calls_strict_shared_contract(sought,tmp_path,mode):
    remote=facade(sought,tmp_path);server=build_server(remote)
    async def run():
        async with Client(server,mode=mode) as client:
            listing=await client.list_tools()
            assert {tool.name for tool in listing.tools}==set(MODELS)
            assert all(tool.annotations.read_only_hint and tool.annotations.open_world_hint is False for tool in listing.tools)
            schema=next(t.input_schema for t in listing.tools if t.name=='search_assets')
            assert schema['additionalProperties'] is False and schema['properties']['limit']['maximum']==50
            response=await client.call_tool('search_assets',{'query':'合成圆圈','filters':{'media_kinds':[]}})
            assert not response.is_error and response.structured_content['items'][0]['asset_id']==sought.asset.asset_id
            assert response.structured_content['current_use_review']=='not_checked'
            for name,args in [('get_asset',{'asset_id':sought.asset.asset_id}),('get_asset_preview',{'asset_id':sought.asset.asset_id,'sha256':sought.asset.sha256}),
                ('get_asset_download',{'asset_id':sought.asset.asset_id,'sha256':sought.asset.sha256})]:
                value=await client.call_tool(name,args);assert not value.is_error
                assert value.structured_content['schema_version']=='d17-files-v1'
                assert 'local_path' not in value.structured_content
            before=len(remote._opener.calls)
            for args in ({'query':'x','user_id':'admin'},{'query':'x','limit':'50'},{'query':'x','evidence_origin':'ocr'}):
                invalid=await client.call_tool('search_assets',args)
                assert invalid.is_error and invalid.structured_content['error']['code']=='invalid_tool_input'
            assert len(remote._opener.calls)==before
            denied=await client.call_tool('publish_terminology',{'data':'synthetic'})
            assert denied.is_error and denied.structured_content['error']['code']=='unknown_read_only_tool'
    asyncio.run(run())


def test_sdk_reports_upstream_denial_as_error_instead_of_no_match(sought,tmp_path):
    server=build_server(facade(sought,tmp_path,token='owner'))
    async def run():
        async with Client(server) as client:
            value=await client.call_tool('search_assets',{'query':'合成圆圈'})
            assert value.is_error and value.structured_content['error']['code']=='reader_credential_required'
            assert 'no_match' not in json.dumps(value.structured_content)
    asyncio.run(run())
