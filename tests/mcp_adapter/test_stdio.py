import asyncio
import json
import os
from pathlib import Path
import sys
import time
import pytest
pytest.importorskip('mcp')
from mcp import Client,StdioServerParameters,stdio_client
from terminology.lexicon.validate_http import server


def alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=wintypes.HANDLE;kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD));kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            code=wintypes.DWORD();return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code)) and code.value==259)
        finally:kernel.CloseHandle(handle)
    import signal
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False


@pytest.mark.parametrize('mode',['2026-07-28','legacy'])
def test_real_sdk_stdio_and_http_boundary_shuts_down_child(sought,tmp_path,mode):
    token=tmp_path/'token.local.txt';token.write_text('reader',encoding='utf8')
    config=tmp_path/'client.local.json';pidfile=tmp_path/'bridge.pid';log=tmp_path/'bridge.stderr.log'
    with server(sought.app) as url:
        config.write_text(json.dumps({'base_url':url,'token_file':str(token)}),encoding='utf8')
        parameters=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('stdio_entry.py')),str(config),str(pidfile)],
            cwd=str(Path.cwd()),env={'PYTHONUTF8':'1'})
        async def run():
            with log.open('w',encoding='utf8') as errors:
                async with Client(stdio_client(parameters,errlog=errors),mode=mode,read_timeout_seconds=10) as client:
                    assert client.protocol_version==('2025-11-25' if mode=='legacy' else mode)
                    assert len((await client.list_tools()).tools)==4
                    value=await client.call_tool('search_assets',{'query':'合成圆圈'})
                    assert not value.is_error and value.structured_content['items'][0]['sha256']==sought.asset.sha256
                    manifest=await client.call_tool('get_asset_download',{'asset_id':sought.asset.asset_id,'sha256':sought.asset.sha256})
                    assert not manifest.is_error and manifest.structured_content['whole_file']
        asyncio.run(run())
    pid=int(pidfile.read_text());deadline=time.monotonic()+5
    while alive(pid) and time.monotonic()<deadline:time.sleep(.05)
    assert not alive(pid)
    assert 'Bearer' not in log.read_text(encoding='utf8')
