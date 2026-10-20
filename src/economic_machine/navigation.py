"""Economic Machine's bounded, read-only navigation adapter.

Exact syntax is evaluated before an optional model. No financial runtime, model,
network, credentials or imports with execution authority are involved here.
The same versioned grammar is consumed by Ticking's browser adapter.
"""
import json
import re
from pathlib import Path
RULES = json.loads(Path(__file__).with_name('navigation_rules.json').read_text())
PATTERNS = [(r, re.compile('^(?:' + r['pattern'] + ')$', re.I)) for r in RULES['patterns']]
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def public_address(value):
    if re.fullmatch(r'0x[a-fA-F0-9]{40}', value):
        return 'evm'
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', value):
        return None
    n = 0
    for c in value:
        n = n * 58 + B58.index(c)
    size = (n.bit_length() + 7) // 8 + len(value) - len(value.lstrip('1'))
    return 'solana' if size == 32 else None

def normalize(query):
    q = re.sub(r'\s+', ' ', query.strip())
    q = re.sub(RULES['prefix'], '', q, flags=re.I)
    return re.sub(RULES['suffix'], '', q, flags=re.I).strip()

def classify(query):
    """Return an exact intent, otherwise None (interpretation required)."""
    q = normalize(query)
    if public_address(q):
        return {'tool':'address', 'asset':q, 'chain':None}
    # Address-shaped invalid inputs are not sent to an external model.
    if re.fullmatch(r'0x[A-Za-z0-9]{20,}|[1-9A-HJ-NP-Za-km-z]{32,100}', q):
        return {'tool':'invalid_address', 'asset':None, 'chain':None}
    for rule, regex in PATTERNS:
        m = regex.fullmatch(q)
        if not m:
            continue
        def group(name):
            return m.group(rule[name]) if rule.get(name) else None
        address, asset, chain = group('addressGroup'), group('assetGroup'), group('chainGroup')
        if address and not public_address(address):
            return {'tool':'invalid_address','asset':None,'chain':None}
        if asset and len(asset)>64 and not public_address(asset):
            return {'tool':'clarify','asset':None,'chain':None}
        return {'tool':rule['tool'], 'asset':address or asset, 'chain':RULES['chains'].get((chain or '').lower())}
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}|[가-힣]{1,16}',q):
        return {'tool':'chart','asset':q,'chain':None}
    return None
