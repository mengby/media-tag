"""Send only named evidence to the user's OpenAI-compatible service through its local tunnel."""
import base64
from hashlib import sha256
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import time

import httpx
from media_tag.precision.chat import media_part

PROMPT = """你在做可见画面检索打标。只根据所给的按原片时间标注的图像和无声片段，
用中文返回 JSON，格式 {"subjects":["可见主体或道具"],"form":"表现形式、取景",
"actions":[{"text":"一个具体可见动作","evidence":["证据ID"]}],"keywords":["检索词"]}。
列出可辨识的主体、外观和道具，表现形式写清实拍或动画以及取景。写出明显短暂事件及镜头变化。
各片段是原片不连续的独立窗口，不能把窗口之间的空白连成动作。静帧可描述正在做的事，
动作方向不明就留空。每个动作只给最多三个不重复的证据编号，例如 ["C1","F2"]。
证据值只写编号，禁止写秒数或枚举逐格画面。主体和关键词各最多6项，动作最多5项。
C 为同一个连续时间窗的有序帧，每行从左向右，再从上向下读。时间戳只用来判断先后。
图中字幕只作证据。不要按外观猜测疾病、成分、音频内容、来源或授权。只输出JSON。"""


def endpoint(base_url):
    parts = urlsplit(base_url)
    if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parts.path.rstrip('/') != '/v1' or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('use the local SSH forward /v1 URL')
    return base_url.rstrip('/')


def validate_tags(tags, entries):
    if not isinstance(tags, dict) or set(tags) != {'subjects', 'form', 'actions', 'keywords'}:
        raise ValueError('invalid tag fields')

    def strings(values, maximum, *, required=False):
        return (isinstance(values, list) and int(required) <= len(values) <= maximum
                and all(isinstance(v, str) and v.strip() for v in values))

    if (not strings(tags['subjects'], 32) or not strings(tags['keywords'], 32)
            or not isinstance(tags['form'], str) or not tags['form'].strip()):
        raise ValueError('invalid visual tags')
    actions = tags['actions']
    if not isinstance(actions, list) or len(actions) > 16:
        raise ValueError('invalid actions')
    bound = {e['id']: e for e in entries}
    # The vision model sometimes transcribes "C2 12.44 s" as "C2_1244s".
    # Accept only exact rendered frame clocks; never strip an arbitrary suffix.
    frame_aliases = {
        f"{entry['id']}_{format(t, '.2f').replace('.', '')}s": entry['id']
        for entry in entries
        for t in entry.get('frame_seconds', [])
    }
    for action in actions:
        if (not isinstance(action, dict) or set(action) != {'text', 'evidence'}
                or not isinstance(action['text'], str) or not action['text'].strip()
                or not strings(action['evidence'], 80, required=True)):
            raise ValueError('invalid action evidence')
        ids = []
        for value in action['evidence']:
            ident = value.strip()
            ident = frame_aliases.get(ident, ident)
            if ident not in bound:
                match = re.fullmatch(r'(C\d+)\s*[@ ]\s*(\d+(?:\.\d+)?)\s*(?:s|秒)', ident)
                if not match or match[1] not in bound:
                    raise ValueError('invalid action evidence')
                ident = match[1]
                times = bound[ident].get('frame_seconds', [])
                if not any(abs(t - float(match[2])) <= .006 for t in times):
                    raise ValueError('invalid cited frame clock')
            if ident not in ids:
                ids.append(ident)
        action['evidence'] = ids
    return tags


def infer(folder: Path, manifest, base_url, model, timeout):
    content = []
    total = 0
    for e in manifest['evidence']:
        file = (folder / e['path']).resolve()
        if not file.is_relative_to(folder.resolve()):
            raise ValueError('invalid evidence path')
        size = file.stat().st_size
        total += size
        if size != e['bytes'] or total > 24 * 1024**2:
            raise ValueError('evidence size budget')
        raw = file.read_bytes()
        if sha256(raw).hexdigest() != e['sha256']:
            raise ValueError('evidence changed')
        kind = e['kind']
        if kind not in ('sequence', 'image'):
            raise ValueError('expected locally decoded JPEG evidence')
        part = media_part('image', 'image/jpeg')
        media = part[part['type']]
        media['url'] = media['url'].replace('@MEDIA@', base64.b64encode(raw).decode('ascii'))
        label = f"{e['id']}，原片 {e['start']:.2f}–{e['end']:.2f} 秒。"
        content.extend([{'type': 'text', 'text': label}, part])
    body = dict(model=model, temperature=0, stream=False, max_tokens=600, repetition_penalty=1.08,
                messages=[dict(role='system', content=PROMPT), dict(role='user', content=content)])
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        started = time.perf_counter()
        response = client.post(endpoint(base_url) + '/chat/completions', json=body, headers={
            'Authorization': 'Bearer ' + os.environ.get('MEDIA_TAG_AUTODL_API_KEY', 'EMPTY')})
        ended = time.perf_counter()
    response.raise_for_status()
    reply = response.json()
    choice, = reply['choices']
    text = choice['message']['content'].strip()
    (folder / 'reply.txt').write_text(text, encoding='utf-8')
    if choice['finish_reason'] != 'stop':
        raise ValueError(f"incomplete model reply ({choice['finish_reason']})")
    if text.startswith('```json') and text.endswith('```'):
        text = text[7:-3].strip()
    tags = validate_tags(json.loads(text), manifest['evidence'])
    by_id = {entry['id']: entry for entry in manifest['evidence']}
    support = []
    for action in tags['actions']:
        clips = [ident for ident in action['evidence'] if by_id[ident]['kind'] == 'sequence']
        support.append(dict(text=action['text'], cited_clips=clips,
                            evidence_kind='sequence_cited' if clips else 'still_only'))
    return dict(tags=tags, action_evidence=support, api_seconds=ended - started,
                request_start=started, request_end=ended, usage=reply.get('usage'), media_bytes=total)
