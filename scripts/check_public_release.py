"""Check a public source snapshot without printing possible credential values."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


PUBLIC_DOCS = {
    'AGENT_CONTROL.md', 'ARCHITECTURE.md', 'CONSOLE.md', 'ECONOMIC_PRIMITIVES.md',
    'ENGINE_ARCHITECTURE.md', 'FUEL.md', 'MACHINE_MINING.md', 'PRODUCTION.md',
    'SELF_HOSTING.md', 'SERVICE_COMMERCE.md', 'SOLUTION_MAINNET.md',
    'SOLUTION_MINING.md', 'SOLUTION_OPERATIONS.md', 'TASK_MCP.md',
    'TASK_PAYMENT_RECOVERY.md', 'UNIFIED_ENGINE.md', 'wallet-connectors.md',
}


def publication_issues(root, paths):
    """Ignored local files cannot satisfy links in an explicit publication set."""
    root = Path(root).resolve()
    published = set(paths)
    failures, checked_links = [], 0
    secret_patterns = [rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                       rb'(?:sk-bk-|sk-proj-|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{24,}',
                       rb'ABSK[A-Za-z0-9+/=]{80,}', rb'https://[^\s/]+\.quiknode\.pro/[A-Za-z0-9]{25,}']
    secret_patterns.extend([rb'ory_rt_[A-Za-z0-9_-]{20,}',
                            rb'eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{30,}\.'])
    for name in paths:
        file = root / name
        if not file.is_file() or not file.resolve().is_relative_to(root):
            failures.append({'path': name, 'reason': 'MISSING_OR_EXTERNAL_SOURCE'})
            continue
        internal_name = re.search(r'(?i)(?:^|[/_.-])(?:PR\d+|DEMO_SCRIPT|ROADMAP|PLAN|PLANNING)(?:[/_.-]|$)', name)
        if (name.startswith(('internal/', 'planning/', 'docs/submission/'))
                or (file.suffix == '.md' and internal_name)
                or (name.startswith('docs/') and name[5:] not in PUBLIC_DOCS)):
            failures.append({'path': name, 'reason': 'INTERNAL_DOCUMENT_IN_PUBLIC_TREE'})
        if name.startswith('artifacts/'):
            failures.append({'path': name, 'reason': 'GENERATED_OUTPUT_IN_PUBLIC_TREE'})
        if file.name.startswith('.env') and file.name != '.env.example' or file.suffix in {'.pem', '.key'}:
            failures.append({'path': name, 'reason': 'PRIVATE_FILE_TYPE'})
        data = file.read_bytes()
        if any(re.search(pattern, data) for pattern in secret_patterns):
            failures.append({'path': name, 'reason': 'CREDENTIAL_PATTERN'})
        if file.suffix != '.md':
            continue
        text = data.decode()
        if name.startswith('docs/') and re.search(r'\bPR[ -]?\d+\b|/Users/[^\s`]+|/srv/skew/[^\s`]*20\d{6}', text):
            failures.append({'path': name, 'reason': 'INTERNAL_WORK_RECORD_IN_GUIDE'})
        for href in re.findall(r'\[[^\]]*\]\(([^) ]+)', text):
            parsed = urlsplit(href)
            if parsed.scheme or not parsed.path or '<' in parsed.path:
                continue
            checked_links += 1
            target = (file.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root):
                included = False
            else:
                relative = target.relative_to(root).as_posix()
                included = relative in published or any(p.startswith(relative.rstrip('/') + '/') for p in published)
            if not target.exists() or not included:
                failures.append({'path': name, 'target': parsed.path, 'reason': 'UNPUBLISHED_DOCUMENT_LINK'})
    result = {'files': len(paths), 'document_links': checked_links, 'failures': failures,
              'scope': 'Current public tree, bounded pattern scan; not a full security audit or Git history erasure.'}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    paths = [p for p in args.manifest.read_text().splitlines() if p]
    result = publication_issues(ROOT, paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(bool(result['failures']))


if __name__ == '__main__':
    main()
