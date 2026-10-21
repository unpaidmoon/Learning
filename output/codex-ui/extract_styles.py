import hashlib
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parent
ARTIFACT = Path(r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1013.4894.0_x64__2p2nqsd0c76g0\app\resources\app.asar')
with ARTIFACT.open('rb') as stream:
    words = struct.unpack('<4I', stream.read(16))
    header = json.loads(stream.read(words[3]))
    base = 8 + words[1]
    entries = {}

    def walk(node, prefix=''):
        for name, item in node.get('files', {}).items():
            path = prefix + name
            if 'files' in item:
                walk(item, path + '/')
            else:
                entries[path] = item

    walk(header)
    output = []
    for path, item in entries.items():
        if not (path.startswith('webview/assets/app-') and path.endswith('.css')):
            continue
        stream.seek(base + int(item['offset']))
        raw = stream.read(item['size'])
        css = raw.decode('utf-8')
        rules = []
        for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
            declarations = dict(re.findall(r'(--[\w-]+)\s*:\s*([^;{}]+)', match[2]))
            selected = {k: v for k, v in declarations.items() if re.search(r'^--(?:color-(?:surface|text|border|background)|font-(?:sans|ui|content)|spacing$|radius-|composer-radius|height-sidebar|spacing-token-sidebar)', k)}
            if selected:
                rules.append({'selector': match[1][-500:], 'declarations': selected})
        output.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'rules': rules})
    record = {'artifact': str(ARTIFACT), 'sha256': hashlib.file_digest(ARTIFACT.open('rb'), 'sha256').hexdigest(), 'scope': 'static CSS declarations; no runtime or computed-style observation', 'styles': output}
    (ROOT / 'style-evidence.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    for style in output:
        for rule in style['rules']:
            values = rule['declarations']
            if '--color-surface' in values or '--color-surface-primary' in values or '--font-sans-default' in values:
                print(json.dumps({'path': style['path'], **rule}, ensure_ascii=False))
