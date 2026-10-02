"""C++ fixed-width candidate gate. No signer, network or dispatch capability."""

import ctypes
import hashlib
import os
from pathlib import Path

from economic_machine.values import MachineError, require_keys

FIELDS = [
    ("version", ctypes.c_uint32), ("side", ctypes.c_uint32),
    *[(name, ctypes.c_uint64) for name in ["sequence", "expected_sequence", "now_ns", "observed_ns", "max_age_ns", "deadline_ns"]],
    *[(name, ctypes.c_int64) for name in ["quantity", "price", "reference_price", "quantity_step", "price_tick",
       "min_notional", "max_order", "available_quote", "available_base", "exposure_after", "max_exposure", "turnover_remaining"]],
    *[(name, ctypes.c_uint32) for name in ["max_slippage_bps", "fee_reserve_bps", "policy_active", "oracle_valid"]],
]


class Input(ctypes.Structure):
    _fields_ = FIELDS


class Output(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("code", ctypes.c_uint32), ("sequence", ctypes.c_uint64),
                ("notional", ctypes.c_int64), ("required_quote", ctypes.c_int64),
                ("valid_until_ns", ctypes.c_uint64), ("execution_authority", ctypes.c_uint64)]


class NativeGate:
    def __init__(self, path=None, expected_hash=None):
        location = path or os.environ.get("ENGINE_NATIVE_LIBRARY")
        checksum = expected_hash or os.environ.get("ENGINE_NATIVE_SHA256")
        self.library = None
        self.sha256 = None
        if not location:
            return
        p = Path(location)
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_size > 20_000_000:
            raise MachineError("TRUSTED_NATIVE_LIBRARY_REQUIRED")
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if not checksum or actual != checksum:
            raise MachineError("NATIVE_LIBRARY_HASH_MISMATCH")
        self.library, self.sha256 = ctypes.CDLL(str(p)), actual
        self.library.machine_abi_version.restype = ctypes.c_uint32
        self.library.machine_input_size.restype = self.library.machine_output_size.restype = ctypes.c_size_t
        self.library.machine_evaluate.argtypes = [ctypes.POINTER(Input), ctypes.POINTER(Output)]
        self.library.machine_evaluate.restype = ctypes.c_int
        if (self.library.machine_abi_version() != 1 or self.library.machine_input_size() != ctypes.sizeof(Input)
                or self.library.machine_output_size() != ctypes.sizeof(Output)):
            raise MachineError("NATIVE_ABI_MISMATCH")

    def status(self):
        return {"enabled": self.library is not None, "abi": 1, "library_sha256": self.sha256,
                "execution_authority": "NONE", "scope": "FIXED_MICROUNIT_CANDIDATE_GATE"}

    def evaluate(self, raw):
        if self.library is None:
            raise MachineError("NATIVE_GATE_NOT_CONFIGURED")
        require_keys(raw, {name for name, _ in FIELDS}, "native input")
        for name, kind in FIELDS:
            value = raw[name]
            low, high = (-(2**63), 2**63 - 1) if kind is ctypes.c_int64 else (0, 2**(ctypes.sizeof(kind) * 8) - 1)
            if type(value) is not int or not low <= value <= high:
                raise MachineError("NATIVE_INTEGER_RANGE")
        state, output = Input(**raw), Output()
        if self.library.machine_evaluate(ctypes.byref(state), ctypes.byref(output)) != 0 or output.execution_authority != 0:
            raise MachineError("NATIVE_AUTHORITY_BOUNDARY_FAILED")
        return {name: getattr(output, name) for name, _ in Output._fields_} | {
            "accepted": output.code == 0, "library_sha256": self.sha256,
            "scope": "CANDIDATE_ONLY_NOT_ORDER_APPROVAL"}
