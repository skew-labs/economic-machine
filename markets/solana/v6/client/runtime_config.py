"""Portable development namespace; no operator configuration is shipped."""
import json, os
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('MP_BASE',str(SOURCE/'runtime'))).resolve()
V6=True
PROGRAM='ChfNY1go7qaJYPK3EJvPZwX99yjEBdnaYzLMgUoiPEa6'
ELF_HASH='6bf2bc652ff4dbe232f1b77021a5ecd40e4e237fd9dac99a28b25aea401497be'
ELF_PATH=Path(os.environ.get('MP_ELF',str(SOURCE/'build/sbf/machine_perps.so')))
PROGRAM_KEY=BASE/'private/program.json'
ACTORS=12
if (BASE/'private/deployment.json').exists():
    deployment=json.loads((BASE/'private/deployment.json').read_text())
    PROGRAM=deployment['program']
