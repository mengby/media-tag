"""Send an authorized file stream to the object selected by the cloud publisher."""
import logging
import re
from urllib.parse import urlsplit

import oss2


def _omit_signed_urls(record):
    return "signature=" not in record.getMessage().lower()


# The SDK and its transport both include the signed URL in debug messages.
for _name in ("oss2.api", "oss2.http", "urllib3.connectionpool"):
    logging.getLogger(_name).addFilter(_omit_signed_urls)


def _reject_redirect(response, **kwargs):
    if 300 <= response.status_code < 400:
        response.close()
        raise ValueError("OSS upload redirect refused")


def upload_object(ticket, ref, open_source):
    if ticket.get("sha256") != ref.sha256:
        raise ValueError("OSS upload ticket digest mismatch")
    if ticket.get("state") == "present":
        return False
    if ticket.get("state") != "upload" or not isinstance(ticket.get("url"), str):
        raise ValueError("invalid OSS upload ticket")

    url = urlsplit(ticket["url"])
    host = re.fullmatch(r"([a-z0-9-]+)\.(oss-[a-z0-9-]+\.aliyuncs\.com)", url.hostname or "")
    if (not host or url.scheme != "https" or url.port not in (None, 443)
            or url.username is not None or url.password is not None or url.fragment or not url.query
            or not url.path.endswith(f"/blobs/{ref.sha256[:2]}/{ref.sha256}")):
        raise ValueError("unexpected OSS upload destination")

    headers = oss2.CaseInsensitiveDict(ticket["headers"])
    headers["Content-Length"] = str(ref.size_bytes)
    session = oss2.Session()
    with session.session as transport:
        transport.trust_env = False
        transport.hooks["response"].append(_reject_redirect)
        bucket = oss2.Bucket(oss2.AnonymousAuth(), "https://" + host[2], host[1],
                             session=session, connect_timeout=120, enable_crc=ref.size_bytes > 0)
        with open_source() as stream:
            # requests treats an empty iterable as chunked even with a signed Content-Length.
            data = oss2.SizedFileAdapter(stream, ref.size_bytes) if ref.size_bytes else b""
            try:
                bucket.put_object_with_url(ticket["url"], data, headers=headers)
            except oss2.exceptions.OssError as exc:
                # Another publisher may have won the same create-only PUT.
                # The cloud still has to verify the object's bytes before adoption.
                if exc.status == 409 and exc.code == "FileAlreadyExists":
                    return False
                raise ValueError(f"OSS upload failed (status {exc.status})") from None
    return True
