"""Portable development namespace; no operator configuration is shipped."""
import json, os
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('MP_BASE',str(SOURCE/'runtime'))).resolve()
V6=True
PROGRAM=None  # Undeployed candidate: no default live program identity.
ELF_HASH='a769c5a45fb380786155f00d465c0f978aca011a32015909282929b2e177953b'
ELF_PATH=Path(os.environ.get('MP_ELF',str(SOURCE/'build/sbf/machine_perps.so')))
PROGRAM_KEY=BASE/'private/program.json'
ACTORS=12
if (BASE/'private/deployment.json').exists():
    deployment=json.loads((BASE/'private/deployment.json').read_text())
    PROGRAM=deployment['program']
