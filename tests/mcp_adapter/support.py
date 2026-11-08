from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from media_tag.client.http import ClientSettings,LibraryClient


class ASGIOpener:
    """HTTP boundary fixture, no socket/model call claimed. Uses the actual authenticated API."""
    def __init__(self,app):self.client=TestClient(app);self.calls=[]
    def open(self,request,timeout=None):
        parsed=urlsplit(request.full_url);path=parsed.path+('?' + parsed.query if parsed.query else '')
        self.calls.append((request.get_method(),path))
        value=self.client.request(request.get_method(),path,content=request.data,headers=dict(request.headers))
        if value.status_code>=400:raise HTTPError(request.full_url,value.status_code,'Synthetic response',value.headers,BytesIO(value.content))
        return BytesIO(value.content)


def facade(sought,tmp_path,token='reader'):
    path=tmp_path/'credential.local.txt';path.write_text(token,encoding='utf8')
    client=LibraryClient(ClientSettings(base_url='http://127.0.0.1:8000',token_file=str(path)))
    client._opener=ASGIOpener(sought.app)
    return client
