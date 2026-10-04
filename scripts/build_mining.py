"""Focused remote C++ build; immutable library and source-bound build evidence."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Build only on the authorized remote host')
    folder = ROOT / 'build/mining'; folder.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'native/src/mining.cpp'
    flags = ['g++', '-std=c++20', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'native/include')]
    test = ROOT / 'native/tests/mining.cpp'
    sanitizer = folder / 'mining-sanitizer'
    subprocess.run(flags + ['-O1', '-g', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', str(test), '-o', str(sanitizer)], check=True, timeout=60)
    sanitizer_result = subprocess.check_output([str(sanitizer)], text=True, timeout=30)
    output = folder / 'libmachine_mining.staged.so'
    subprocess.run(flags + ['-O3', '-fPIC', '-shared', str(source), '-o', str(output)], check=True, timeout=60)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    target = folder / f'libmachine_mining-{sha}.so'; output.replace(target)
    sources = [source, test, ROOT / 'native/include/machine/economics/amm.hpp', ROOT / 'native/include/machine/economics/value.hpp']
    proof = {'abi': 1, 'compiler': subprocess.check_output(['g++', '--version'], text=True).splitlines()[0],
             'sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
             'library_sha256': sha, 'library_path': str(target), 'authority': 'CANDIDATE_ONLY',
             'sanitizers': 'ADDRESS_UNDEFINED', 'sanitizer_result': sanitizer_result.strip(),
             'arithmetic': 'INTEGER_CEIL_INPUT_FEE_CONSTANT_PRODUCT_FROZEN_MODEL'}
    evidence = ROOT / 'artifacts/mining'; evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'native-build.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof))


if __name__ == '__main__':
    main()
