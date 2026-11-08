from io import BytesIO
import json
import pytest
from media_tag.client.http import ClientSettings,LibraryClient,LibraryClientError,NoRedirect,load_settings
from media_tag.search.public import AssetSearch
from .support import facade


def test_client_reuses_strict_api_and_fixed_manifest_without_local_path_claims(sought,tmp_path):
    client=facade(sought,tmp_path)
    result=client.search(AssetSearch(query='合成圆圈'))
    item=result['items'][0]
    assert item['asset_id']==sought.asset.asset_id and item['current_use_review']=='not_checked'
    assert client.asset(item['asset_id'])['sha256']==item['sha256']
    assert client.manifest(item['asset_id'],item['sha256'])['whole_file']
    assert not client.manifest(item['asset_id'],item['sha256'],preview=True)['full_video_observed']
    assert all(path.startswith('/v1/') for _,path in client._opener.calls)
    assert all(client._opener.calls[n][1]=='/v1/capabilities' for n in (0,2,4,6))


def test_editor_credential_and_arbitrary_paths_cannot_be_used_by_bridge(sought,tmp_path):
    client=facade(sought,tmp_path,token='owner')
    with pytest.raises(LibraryClientError,match='reader_credential_required'):client.search(AssetSearch(query='x'))
    for path in ('https://outside.invalid/v1/assets/x','//outside.invalid/v1/assets/x','/v1/../jobs'):
        with pytest.raises(LibraryClientError,match='untrusted_api_path'):client._request('GET',path)
    with pytest.raises(LibraryClientError,match='untrusted_api_path'):client._request('POST','/v1/any-write')
    with pytest.raises(LibraryClientError,match='invalid_asset_id'):client.asset('../admin')
    with pytest.raises(LibraryClientError,match='invalid_fixed_version'):client.manifest('asset','not-sha')
    assert len(client._opener.calls)==1


def test_settings_do_not_accept_http_remote_credentials_or_relative_secret_files(tmp_path):
    path=tmp_path/'token.local.txt';path.write_text('synthetic-private-token')
    for base in ('http://example.invalid','https://user:password@example.invalid','https://example.invalid/path','https://example.invalid?token=secret'):
        with pytest.raises(ValueError):ClientSettings(base_url=base,token_file=str(path))
    with pytest.raises(ValueError):ClientSettings(base_url='http://127.0.0.1',token_file='relative.txt')
    with pytest.raises(LibraryClientError,match='redirect_refused'):NoRedirect().redirect_request(None,None,None,None,None,None)
    cfg=tmp_path/'client.local.json';cfg.write_text(json.dumps({'base_url':'http://127.0.0.1:8000','token_file':str(path)}))
    assert load_settings(cfg).response_limit_bytes==4*1024**2
    path.write_text('synthetic-private-token'+' '*4096)
    with pytest.raises(LibraryClientError,match='invalid_client_credential'):LibraryClient(load_settings(cfg))


def test_response_size_schema_timeout_and_server_errors_are_separate_and_sanitized(sought,tmp_path):
    client=facade(sought,tmp_path)
    class Huge:
        def open(self,*args,**kwargs):return BytesIO(b' '* (4*1024**2+1))
    client._opener=Huge()
    with pytest.raises(LibraryClientError,match='byte_budget'):client._request('GET','/v1/capabilities')
    class Timed:
        def open(self,*args,**kwargs):raise TimeoutError('synthetic secret must not leak')
    client._opener=Timed()
    with pytest.raises(LibraryClientError) as timed:client._request('GET','/v1/capabilities')
    assert timed.value.code=='upstream_timeout' and timed.value.retryable and 'secret' not in str(timed.value)
    from urllib.error import HTTPError
    class Denied:
        def open(self,*args,**kwargs):raise HTTPError('https://private.invalid',403,'synthetic secret',None,BytesIO(b'{"error":{"code":"file_access_denied","retryable":false}}'))
    client._opener=Denied()
    with pytest.raises(LibraryClientError) as denied:client._request('GET','/v1/capabilities')
    assert denied.value.payload()=={'error':{'code':'file_access_denied','status':403,'retryable':False}}
    class Invalid:
        def open(self,*args,**kwargs):return BytesIO(b'<html>not catalogue JSON</html>')
    client._opener=Invalid()
    with pytest.raises(LibraryClientError,match='invalid_response'):client._request('GET','/v1/capabilities')


def test_fixed_version_response_mismatch_and_request_budget_are_rejected(sought,tmp_path):
    client=facade(sought,tmp_path)
    def reply(method,path,payload=None):
        if path=='/v1/capabilities':return {'schema_version':'d17-api-v1','fixed_version_files':True,'read_only_identity':True}
        return {'schema_version':'d17-files-v1','asset_id':sought.asset.asset_id,'sha256':'f'*64}
    client._request=reply
    with pytest.raises(LibraryClientError,match='fixed_version_mismatch'):client.manifest(sought.asset.asset_id,sought.asset.sha256)
    client=facade(sought,tmp_path)
    with pytest.raises(LibraryClientError,match='request_byte_budget'):client._request('POST','/v1/assets/search',{'query':'x'*(129*1024)})


def test_bad_cli_config_does_not_print_secrets_to_protocol_or_diagnostics(tmp_path):
    import subprocess
    import sys
    path=tmp_path/'bad-client.local.json'
    path.write_text(json.dumps({'base_url':'https://user:must-not-print@example.invalid','token_file':str(tmp_path/'missing-token')}))
    result=subprocess.run([sys.executable,'-m','media_tag','mcp-stdio','--config',str(path)],capture_output=True,timeout=10)
    assert result.returncode==2 and result.stdout==b''
    assert json.loads(result.stderr)=={'error':'mcp_bridge_failed'} and b'must-not-print' not in result.stderr
