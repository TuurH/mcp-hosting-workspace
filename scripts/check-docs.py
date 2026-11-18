"""Check local Markdown links, heading anchors and referenced PowerShell commands."""
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md')), *sorted((ROOT / 'examples').rglob('README.md'))]


def anchors(path):
    result = set()
    counts = {}
    fenced = False
    for line in path.read_text().splitlines():
        if line.startswith('```'):
            fenced = not fenced
        if fenced or not re.match(r'^#{1,6} ', line):
            continue
        heading = re.sub(r'^#+ ', '', line).strip().lower()
        slug = ''.join(c for c in heading if c.isalnum() or c in ' _-').replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(slug + (f'-{count}' if count else ''))
    return result


failures = []
for doc in DOCS:
    content = doc.read_text()
    for url in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', content):
        if re.match(r'^[a-z]+:', url):
            continue
        relative, _, anchor = unquote(url).partition('#')
        target = (doc.parent / relative).resolve() if relative else doc
        if not target.exists():
            failures.append(f'{doc.relative_to(ROOT)}: missing {url}')
        elif anchor and target.suffix == '.md' and anchor not in anchors(target):
            failures.append(f'{doc.relative_to(ROOT)}: missing heading {url}')
    for script in re.findall(r'\.\\scripts\\([A-Za-z0-9_-]+\.ps1)', content):
        if not (ROOT / 'scripts' / script).is_file():
            failures.append(f'{doc.relative_to(ROOT)}: missing script {script}')
if failures:
    print('\n'.join(failures))
    sys.exit(1)
print(f'{len(DOCS)} guides: local links, heading anchors and PowerShell script paths passed.')
