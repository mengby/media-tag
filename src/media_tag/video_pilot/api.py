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
from .still_sheet import make_still_sheet

JSON_FORMAT = """用中文返回 JSON，格式 {"subjects":["可见主体或道具"],"form":"表现形式、取景",
"actions":[{"text":"一个具体可见动作","evidence":["证据ID"]}],"keywords":["检索词"]}。只输出JSON。"""

PROMPT = """你在做可见画面检索打标。只根据所给的按原片时间标注的图像和无声片段，用中文打标。
列出可辨识的主体、外观和道具，表现形式写清实拍或动画以及取景。写出明显短暂事件及镜头变化。
各片段是原片不连续的独立窗口，不能把窗口之间的空白连成动作。静帧可描述正在做的事，
动作方向不明就留空。每个动作只给最多三个不重复的证据编号，例如 ["C1","F2"]。
证据值只写编号，禁止写秒数或枚举逐格画面。主体和关键词各最多6项，动作最多5项。
C 为同一个连续时间窗的有序帧，每行从左向右，再从上向下读。时间戳只用来判断先后。
图中字幕只作证据。不要按外观猜测疾病、成分、音频内容、来源或授权。"""

COVERAGE_GUIDE = """
F 是分别取自不同时刻的独立大图。先逐张看 F，subjects 写可直接看清的具体物体和局部结构，
用可见的颜色、形状、所在位置或搭配的器具区分主体。把修饰词和对应物体写在同一短语中，
避开只能猜测的物种、材质和成分。form 写与画面一致的制作方式和景别。
逐个看 C 中相邻格：检查主体位置、朝向和局部形状是否随时间改变、是否重复变化。
动作写清哪个可见物体怎样变化，区分物体运动和镜头运动；没有依据就省略。
每个变化至少引用一个能看到该过程的连续 C 窗口，不能凭两张 F 之间的差异推断动作。"""


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


def visual_content(folder, manifest):
    visuals, stills = [], []
    edge = manifest.get('still_sheet_edge')
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
        if edge is not None and kind == 'image':
            stills.append((e, raw))
        else:
            visuals.append((f"{e['id']}，原片 {e['start']:.2f}–{e['end']:.2f} 秒。", raw))
    if edge is not None:
        sheet = make_still_sheet(folder, stills, edge)
        labels = '，'.join(f"{e['id']} {e['start']:.2f} 秒" for e, _ in stills)
        visuals.insert(0, (f'独立静帧拼图，从左到右、从上到下依次为 {labels}。各格取自不连续时刻。', sheet))

    sent_bytes = sum(len(raw) for _, raw in visuals)
    if sent_bytes > 24 * 1024**2:
        raise ValueError('request image size budget')
    content = []
    for label, raw in visuals:
        part = media_part('image', 'image/jpeg')
        media = part[part['type']]
        media['url'] = media['url'].replace('@MEDIA@', base64.b64encode(raw).decode('ascii'))
        content.extend([{'type': 'text', 'text': label}, part])
    if visuals and edge is None and manifest.get('still_edge') == 320:
        ids = '、'.join(e['id'] for e in manifest['evidence'])
        example = next((e['id'] for e in manifest['evidence'] if e['kind'] == 'sequence'), manifest['evidence'][0]['id'])
        content.append({'type': 'text', 'text': f'本条 actions 的 evidence 可用编号：{ids}。'
                        f'直接复制编号，例如 ["{example}"]。秒数只用于看图，不写入编号。'})
    return content, sent_bytes, len(visuals)


def parse_tag_reply(text, entries):
    # Some servers return a closing Markdown fence without its opening fence.
    text = text.strip()
    if text.endswith('```'):
        text = text[:-3].rstrip()
        text = re.sub(r'^```(?:json)?\s*', '', text, count=1, flags=re.IGNORECASE)
    return validate_tags(json.loads(text), entries)


def parse_tag_lines(text, entries):
    # A second representation of the same required fields for format retries.
    tags = {}
    actions = []
    lines = text.strip().splitlines()
    if not lines or lines[-1].strip() != 'end':
        raise ValueError('missing end of tag lines')
    for line in lines[:-1]:
        parts = [part.strip() for part in line.split('|')]
        field, values = parts[0], parts[1:]
        if field == 'action' and len(values) >= 2:
            evidence = values[1:]
            if len(evidence) == 1 and evidence[0].startswith('['):
                evidence = json.loads(evidence[0])
            actions.append({'text': values[0], 'evidence': evidence})
        elif field in ('subjects', 'keywords') and field not in tags:
            tags[field] = values
        elif field == 'form' and values and all(values) and field not in tags:
            tags[field] = '，'.join(values)
        else:
            raise ValueError('invalid tag lines')
    tags['actions'] = actions
    return validate_tags(tags, entries)


def infer(folder: Path, manifest, base_url, model, timeout):
    content, total, sent_images = visual_content(folder, manifest)
    prompt = PROMPT + COVERAGE_GUIDE if manifest.get('method') == 'coverage' else PROMPT
    ids = '、'.join(e['id'] for e in manifest['evidence'])
    compact = ('综合全部画面，只写一份检索结果。优先最有辨识度的主体与检索词，各至多6个短语。'
               '同一动作出现多次只写一次，最多5项。无可见动作写空数组。form 写可见的表现形式。'
               f'动作证据只用这些原样编号：{ids}。只返回一个完整 JSON 对象，到右花括号结束。')
    line_format = ('重新查看同一批证据。沿用所有画面与证据要求，本次以逐行格式返回，使用英文竖线分隔。'
                   '第一行 subjects|主体1|主体2，第二行 form|表现形式，第三行 keywords|词1|词2。'
                   '主体和检索词各最多4个，空列表只写该字段名。接着每个不同的可见动作单写一行 '
                   'action|动作短语|证据编号，最多3行。无依据则省略动作行。最后单独写 end。'
                   f'仅可引用这些编号：{ids}。禁止写时间、示例占位词、围栏及解释。')
    body = dict(model=model, temperature=0, stream=False, max_tokens=1200, repetition_penalty=1.08)
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        started = time.perf_counter()
        for attempt, (instruction, parser) in enumerate(((compact, parse_tag_reply), (line_format, parse_tag_lines))):
            system = prompt if attempt else prompt + '\n' + JSON_FORMAT
            body['messages'] = [dict(role='system', content=system),
                                dict(role='user', content=[*content, dict(type='text', text=instruction)])]
            response = client.post(endpoint(base_url) + '/chat/completions', json=body, headers={
                'Authorization': 'Bearer ' + os.environ.get('MEDIA_TAG_AUTODL_API_KEY', 'EMPTY')})
            response.raise_for_status()
            reply = response.json()
            choice, = reply['choices']
            text = choice['message']['content'].strip()
            (folder / 'reply.txt').write_text(text, encoding='utf-8')
            reason = choice['finish_reason']
            if reason not in ('stop', 'length'):
                raise ValueError(f'incomplete model reply ({reason})')
            try:
                if reason == 'length':
                    raise ValueError('incomplete model reply (length)')
                tags = parser(text, manifest['evidence'])
                break
            except ValueError:
                if attempt:
                    raise
                (folder / 'reply-first.txt').write_text(text, encoding='utf-8')
        ended = time.perf_counter()
    by_id = {entry['id']: entry for entry in manifest['evidence']}
    support = []
    for action in tags['actions']:
        clips = [ident for ident in action['evidence'] if by_id[ident]['kind'] == 'sequence']
        support.append(dict(text=action['text'], cited_clips=clips,
                            evidence_kind='sequence_cited' if clips else 'still_only'))
    return dict(tags=tags, action_evidence=support, api_seconds=ended - started,
                request_start=started, request_end=ended, usage=reply.get('usage'), media_bytes=total,
                sent_images=sent_images, model_calls=attempt + 1)
