"""Account-bound sub2api image execution; unknown acceptance never causes a re-POST."""
import base64
import io
import json
import os
import re
from contextlib import nullcontext

import httpx
from PIL import Image

from .image import image, MAX_BYTES
from .profile import origin
from .proxy import ProxyUnavailable
from .store import Conflict, now

# Compatibility identifiers from Image's existing CodexProvider, not new model claims.
MODEL = 'gpt-image-2.5-sunburst'
MODELS = {MODEL, 'gpt-image-2.5-flare', 'gpt-image-2'}
INSTRUCTIONS = (
    "When invoking the image_generation tool, use the user's image prompt verbatim. "
    "Do not rewrite, expand, summarize, embellish, translate, normalize punctuation, "
    "or add or remove visual details or constraints. Preserve the original language, "
    "wording, capitalization, quotes, and punctuation exactly."
)
NEGATIVE_BLOCK = re.compile(
    r'(?:^|\r?\n)\s*(?:负面提示词|负向提示词|反向提示词|negative\s*prompts?)[\s:：].*?(?=(?:\r?\n\s*\r?\n)|\Z)',
    re.IGNORECASE | re.DOTALL,
)
SIZES_4K = {'2048x1152': '3840x2160', '1152x2048': '2160x3840', '1024x1024': '2048x2048',
            '1792x1344': '2880x2160', '1344x1792': '2160x2880',
            '1536x1024': '3072x2048', '1024x1536': '2048x3072'}


def payload(value):
    if not isinstance(value, dict) or set(value) - {
            'prompt', 'action', 'size', 'prefer_4k', 'quality', 'background', 'images', 'mask', 'instructions'}:
        raise ValueError('invalid_payload')
    if not isinstance(value.get('prompt'), str) or not value['prompt'].strip() or len(value['prompt']) > 32000:
        raise ValueError('invalid_prompt')
    action, size = value.get('action', 'generate'), value.get('size', '2048x1152')
    if action not in {'generate', 'edit'} or not isinstance(size, str) or not re.fullmatch(r'[1-9][0-9]{0,3}x[1-9][0-9]{0,3}', size):
        raise ValueError('unsupported_parameter')
    width, height = map(int, size.split('x'))
    if max(width, height) > 4096 or width * height > 16 * 1024 * 1024:
        raise ValueError('unsupported_size')
    prefer = value.get('prefer_4k', False)
    if type(prefer) is not bool:
        raise ValueError('invalid_prefer_4k')
    quality, background = value.get('quality', 'high'), value.get('background', 'auto')
    if quality not in {'auto', 'low', 'medium', 'high', 'xhigh', 'max', 'hd', 'standard'} or background not in {'auto', 'opaque', 'transparent'}:
        raise ValueError('unsupported_parameter')
    instructions = value.get('instructions')
    if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 32000):
        raise ValueError('invalid_instructions')
    images = value.get('images', [])
    if not isinstance(images, list) or len(images) > 12 or (action == 'generate' and (images or value.get('mask'))) or (action == 'edit' and not images):
        raise ValueError('invalid_references')
    def checked(item):
        result = image(item, source=True)
        return {key: result[key] for key in ('mime_type', 'data')}
    return {'prompt': value['prompt'], 'action': action, 'size': size, 'prefer_4k': prefer,
            'quality': {'hd': 'high', 'standard': 'medium'}.get(quality, quality), 'background': background,
            'images': [checked(item) for item in images],
            'mask': checked(value['mask']) if value.get('mask') is not None else None,
            'instructions': instructions}


def final_image(data, *, sse=False):
    """Only complete image results count; partial stream frames are never final."""
    encoded = None
    if sse:
        for event in data:
            item = event.get('item', {})
            if event.get('type') in {'response.output_item.done', 'output_item.done'}:
                if item.get('type') == 'image_generation_call' and item.get('status') == 'completed':
                    encoded = item.get('result')
            if event.get('type') == 'response.completed':
                response = event.get('response', {})
                for item in response.get('output', []):
                    if item.get('type') == 'image_generation_call' and item.get('status') == 'completed':
                        encoded = item.get('result')
    else:
        items = data.get('data')
        if isinstance(items, list) and len(items) == 1:
            encoded = items[0].get('b64_json')
    if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
        raise ValueError('missing_final_image')
    decoded = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(decoded)) as output:
        mime = Image.MIME.get(output.format)
    return image({'mime_type': mime, 'data': encoded})


def read_result(response, streaming):
    raw = bytearray()
    for part in response.iter_bytes():
        raw.extend(part)
        if len(raw) > 128 * 1024 * 1024:
            raise ValueError('response_too_large')
    if not streaming:
        return final_image(json.loads(raw))
    events, lines, event_type = [], [], ''
    def flush():
        if not lines:
            return
        text = '\n'.join(lines)
        if text != '[DONE]':
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError('invalid_event')
            if event_type:
                value.setdefault('type', event_type)
            events.append(value)
    for line in raw.decode('utf-8').splitlines() + ['']:
        if not line:
            flush()
            lines, event_type = [], ''
        elif line.startswith('event:'):
            event_type = line[6:].strip()
        elif line.startswith('data:'):
            lines.append(line[5:].strip())
    return final_image(events, sse=True)


class Execution:
    def __init__(self, calls, client, *, transport=None):
        self.calls, self.client, self.transport = calls, client, transport
        self.models = set(os.environ.get('CHATGPT_IMAGE_MODELS', MODEL).split(','))
        if not self.models or not self.models <= MODELS:
            raise ValueError('invalid_image_models')
        self.prefer_responses = os.environ.get('CHATGPT_PREFER_RESPONSES', 'false').lower() in {'true', '1', 'yes'}
        self.sanitize = os.environ.get('CHATGPT_AUTO_SANITIZE_NEGATIVE_PROMPTS', 'true').lower() in {'true', '1', 'yes'}
        self.session_model = os.environ.get('CHATGPT_SESSION_MODEL', 'gpt-5.6-luna')
        self.actor_auth = os.environ.get('CHATGPT_ACTOR_AUTH', 'local-image-extension')
        with calls.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS channel_results ('
                       'call_id TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, created_at TEXT NOT NULL)')

    def request(self, value, model):
        size = value['size']
        if value['prefer_4k']:
            width, height = map(int, size.split('x'))
            size = SIZES_4K.get(size, '2160x3840' if width < height else '2048x2048' if width == height else '3840x2160')
        prompt = re.sub(r'\n{3,}', '\n\n', NEGATIVE_BLOCK.sub('\n', value['prompt'])).strip() if self.sanitize else value['prompt']
        if self.prefer_responses and value['mask'] is None:
            content = [{'type': 'input_image', 'image_url': 'data:' + item['mime_type'] + ';base64,' + item['data']} for item in value['images']]
            content.append({'type': 'input_text', 'text': prompt})
            tool = {'type': 'image_generation', 'action': value['action'], 'model': model, 'size': size,
                    'quality': 'high' if value['quality'] == 'auto' else value['quality'],
                    'background': value['background'], 'output_format': 'png', 'partial_images': 3}
            return '/responses', {'json': {'model': self.session_model, 'instructions': value['instructions'] or INSTRUCTIONS,
                'input': [{'type': 'message', 'role': 'user', 'content': content}], 'tools': [tool],
                'tool_choice': {'type': 'image_generation'}, 'stream': True}}, True
        fields = {'model': model, 'prompt': prompt, 'size': size}
        if value['background'] != 'auto':
            fields['background'] = value['background']
        if value['action'] == 'generate':
            fields.update(quality=value['quality'], response_format='b64_json')
            return '/images/generations', {'json': fields}, False
        fields.update(output_format='png', n='1')
        if value['quality'] != 'auto':
            fields['quality'] = value['quality']
        files = [('image' if index == 0 else 'image_' + str(index),
                  ('source.png' if index == 0 else 'source_' + str(index) + '.png', base64.b64decode(item['data']), item['mime_type']))
                 for index, item in enumerate(value['images'])]
        if value['mask']:
            files.append(('mask', ('mask.png', base64.b64decode(value['mask']['data']), value['mask']['mime_type'])))
        return '/images/edits', {'data': fields, 'files': files}, False

    def submit(self, organization, caller, operation, task, model, value, account, ceiling):
        if model not in self.models:
            raise ValueError('capability_not_supported')
        value = payload(value)
        row, fresh = self.calls.reserve(organization, caller, operation, task, model, value, account, ceiling, action=value['action'])
        if not fresh:
            return self.calls.project(row)
        try:
            base, key = origin(row['api_url']), self.calls.credential(row)
            endpoint, arguments, streaming = self.request(value, model)
        except (ValueError, Conflict):
            self.calls.rejected(row['id'])
            raise
        headers = {'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream, application/json' if streaming else 'application/json'}
        if streaming and self.actor_auth:
            headers['x-openai-actor-authorization'] = self.actor_auth
        try:
            if self.transport is None and json.loads(row['proxy_json']) != {'mode': 'direct'}:
                raise ProxyUnavailable()
            with self.transport(organization, json.loads(row['proxy_json'])) if self.transport else nullcontext(self.client) as client:
                with client.stream('POST', base + endpoint, headers=headers, timeout=300,
                                   follow_redirects=False, **arguments) as response:
                    if response.status_code in {400, 401, 403, 422}:
                        self.calls.rejected(row['id'])
                    elif response.is_success:
                        self.finish(row['id'], read_result(response, streaming))
        except ProxyUnavailable:
            self.calls.rejected(row['id'])
            raise Conflict('proxy_unavailable') from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError, Image.DecompressionBombError):
            pass
        return self.calls.project(self.calls.get(organization, row['id'], caller))

    def finish(self, call_id, output):
        timestamp = now()
        with self.calls.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM channel_calls WHERE id=?', (call_id,)).fetchone()
            if row is None or row['state'] != 'acceptance_unknown':
                return
            encrypted = self.calls.store.cipher.encrypt(json.dumps(output).encode())
            db.execute('INSERT INTO channel_results VALUES (?,?,?)', (call_id, encrypted, timestamp))
            db.execute("UPDATE channel_calls SET state='completed',accepted=1,updated_at=?,finished_at=?,revision=revision+1 WHERE id=?",
                       (timestamp, timestamp, call_id))

    def result(self, organization, caller, call_id):
        row = self.calls.get(organization, call_id, caller)
        with self.calls.store.connect() as db:
            result = db.execute('SELECT ciphertext FROM channel_results WHERE call_id=?', (call_id,)).fetchone()
        if result is None:
            raise Conflict('result_not_available')
        return {**self.calls.project(row), 'image': json.loads(self.calls.store.cipher.decrypt(result['ciphertext']))}
