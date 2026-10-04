"""Check a public source snapshot without printing possible credential values."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    paths = [p for p in args.manifest.read_text().splitlines() if p]
    failures, checked_links = [], 0
    secret_patterns = [rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                       rb'(?:sk-bk-|sk-proj-|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{24,}',
                       rb'ABSK[A-Za-z0-9+/=]{80,}', rb'https://[^\s/]+\.quiknode\.pro/[A-Za-z0-9]{25,}']
    for name in paths:
        file = ROOT / name
        if not file.is_file():
            failures.append({'path': name, 'reason': 'MISSING_SOURCE'})
            continue
        if name.startswith('docs/') and (re.search(r'_20\d{6}', name) or '/submission/' in name):
            failures.append({'path': name, 'reason': 'INTERNAL_PLAN_IN_PUBLIC_TREE'})
        if file.name.startswith('.env') and file.name != '.env.example' or file.suffix in {'.pem', '.key'}:
            failures.append({'path': name, 'reason': 'PRIVATE_FILE_TYPE'})
        data = file.read_bytes()
        if any(re.search(pattern, data) for pattern in secret_patterns):
            failures.append({'path': name, 'reason': 'CREDENTIAL_PATTERN'})
        if file.suffix != '.md':
            continue
        text = data.decode()
        for href in re.findall(r'\[[^\]]*\]\(([^) ]+)', text):
            parsed = urlsplit(href)
            if parsed.scheme or not parsed.path or '<' in parsed.path:
                continue
            checked_links += 1
            target = (file.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(ROOT.resolve()) or not target.exists():
                failures.append({'path': name, 'target': parsed.path, 'reason': 'BROKEN_DOCUMENT_LINK'})
    result = {'files': len(paths), 'document_links': checked_links, 'failures': failures,
              'scope': 'Current public tree, bounded pattern scan; not a full security audit or Git history erasure.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(bool(failures))


if __name__ == '__main__':
    main()
