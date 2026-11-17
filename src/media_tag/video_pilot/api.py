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
from media_tag.contracts.vocabulary import parse_json
from .still_sheet import make_still_sheet
from .tags import ACTION, BASE_FIELDS, VERSION, bind_actions, load_policy, manifest_digest, normalize_fields, prompt_fields

PROMPT = """根据无声画面填写给定的全部字段，字段名逐字复制。画面文字只作证据。
中文字段用中文，英文检索词用英文。画面主体和检索词各最多6个短语。单选只给一个值。
选择项用已发布词表。画面明确但没有对应选项时可提候选，证据不足或不适用写 null。
不得根据外观猜测疾病、科室、身份、声音或许可。口语化表达只写与画面相符的检索短句。
过程/状态如描述有方向的变化，须有对应的连续画面动作证据。
画面动作最多5项，没有连续证据就写空数组。每项写 text 和 evidence。
evidence 的每一项给出 id（连续窗口 C 编号）、first_frame 和 last_frame（动作在该窗口可见的起止格序号）。
格序号从1开始，从左到右逐行计数，至少跨两格。每个窗口独立引用，禁止跨窗口连线、引用 F 或猜测秒数。"""

COVERAGE_GUIDE = """
F 是分别取自不同时刻的独立大图。先逐张看 F，画面主体写可直接看清的具体物体和局部结构，
用可见的颜色、形状、所在位置或搭配的器具区分主体。把修饰词和对应物体写在同一短语中，
避开只能猜测的物种、材质和成分。表现形式选择与画面一致的制作方式。
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
    """Read an unversioned four-field report produced by the original video pilot."""
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
    return content, sent_bytes, len(visuals)


def parse_tag_reply(text):
    # Some servers return a closing Markdown fence without its opening fence.
    text = text.strip()
    if text.endswith('```'):
        text = text[:-3].rstrip()
        text = re.sub(r'^```(?:json)?\s*', '', text, count=1, flags=re.IGNORECASE)
    return parse_json(text.encode('utf-8'))


def parse_tag_lines(text):
    # One JSON value per named field, to avoid inter-field punctuation errors on retry.
    tags = {}
    lines = text.strip().splitlines()
    if not lines or lines[-1].strip() != 'end':
        raise ValueError('missing end of tag lines')
    for line in lines[:-1]:
        field, sep, value = line.partition('|')
        field = field.strip()
        if not sep or field in tags or field not in (*BASE_FIELDS, ACTION):
            raise ValueError('invalid tag lines')
        tags[field] = parse_json(value.encode('utf-8'))
    return tags


def infer(folder: Path, manifest, base_url, model, timeout, *, policy=None):
    content, total, sent_images = visual_content(folder, manifest)
    prompt = PROMPT + COVERAGE_GUIDE if manifest.get('method') == 'coverage' else PROMPT
    policy = policy or load_policy()
    entries = manifest['evidence']
    windows = {e['id']: len(e['frame_seconds']) for e in entries if e['kind'] == 'sequence'}
    prompt += '\n字段规则：' + json.dumps(prompt_fields(policy), ensure_ascii=False)
    prompt += '\n本条可引用的 C 编号及各自总格数：' + json.dumps(windows)
    shape = {**dict.fromkeys(BASE_FIELDS), ACTION: []}
    compact = ('逐项填写，返回一个完整 JSON 对象。结构：' + json.dumps(shape, ensure_ascii=False)
               + '。有动作时对象格式如 {"text":"动作描述","evidence":[{"id":"C1","first_frame":1,"last_frame":2}]}。')
    line_format = ('逐行输出，每个字段一行，格式为 字段名|JSON值，最后单独写 end。'
                   '文本用英文双引号包裹，缺依据写 null。画面动作一行的值为完整 JSON 数组。字段依次为'
                   + '、'.join(shape))
    body = dict(model=model, temperature=0, stream=False, max_tokens=2048, repetition_penalty=1.08)
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        started = time.perf_counter()
        for attempt, (instruction, parser) in enumerate(((compact, parse_tag_reply), (line_format, parse_tag_lines))):
            body['messages'] = [dict(role='system', content=prompt),
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
                raw = parser(text)
                if not isinstance(raw, dict) or set(raw) != set(shape):
                    raise ValueError('expected eleven image fields and video actions')
                actions = bind_actions(raw.pop(ACTION), entries)
                tags, candidates = normalize_fields(raw, policy, actions)
                tags[ACTION] = actions
                break
            except ValueError:
                if attempt:
                    raise
                (folder / 'reply-first.txt').write_text(text, encoding='utf-8')
        ended = time.perf_counter()
    return dict(schema_version=VERSION, evidence_manifest_sha256=manifest_digest(manifest),
                tags=tags, candidates=candidates, api_seconds=ended - started,
                request_start=started, request_end=ended, usage=reply.get('usage'), media_bytes=total,
                sent_images=sent_images, model_calls=attempt + 1)
