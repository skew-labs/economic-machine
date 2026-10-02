"""Portable ISA specification and generated conformance contract."""

import copy
import json
import unittest
from pathlib import Path

from economic_machine.compiler import compile_program
from economic_machine.kernel import EconomicKernel
from economic_machine.spec import SPEC, SPEC_HASH, conformance_vectors, validate_spec
from economic_machine.transitions import transition
from economic_machine.values import MachineError, digest


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "cases" / "economic_isa_vectors_v1.json"


class EconomicSpecTests(unittest.TestCase):
    def test_existing_isa_receipts_keep_their_exact_identity(self):
        world = json.loads((ROOT / "cases" / "economic_state_demo.json").read_text())
        expected = {
            "economic_program_demo.json": "bc2d07e53c2c131b1627bb522df6db7ca9685ee2d34d7bb6dfd61869282b33af",
            "economic_program_guarded_demo.json": "0ca1b1ab2003e69afbb728242fac6cf1f5c6da931ab8c63f4b25a35c3f26885e",
            "economic_program_graph_demo.json": "6092e428fe9dfaf3ac475f99d7171f4039782d2982b3eb43bb149596ccdd4aa9",
        }
        for filename, receipt_hash in expected.items():
            with self.subTest(program=filename):
                program = json.loads((ROOT / "cases" / filename).read_text())
                actual = EconomicKernel().evaluate(compile_program(program), world,
                                                   at="2026-09-25T12:01:00+00:00")
                self.assertEqual(actual["receipt_hash"], receipt_hash)

    def test_exported_portable_vectors_are_pinned_and_replay_on_python_runtime(self):
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.assertEqual(corpus["schema_version"], "economic-isa-conformance-1")
        self.assertEqual(corpus["spec_hash"], SPEC_HASH)
        self.assertEqual(corpus["spec_hash"], digest(SPEC))
        self.assertEqual(corpus["vectors"], conformance_vectors())
        self.assertEqual(len(corpus["vectors"]), 195)
        for vector in corpus["vectors"]:
            with self.subTest(phase=vector["phase"], opcode=vector["opcode"]):
                if vector["next_phase"] is None:
                    with self.assertRaises(MachineError):
                        transition(vector["phase"], vector["opcode"])
                else:
                    self.assertEqual(transition(vector["phase"], vector["opcode"]),
                                     vector["next_phase"])

    def test_spec_rejects_unsafe_phase_or_reserved_opcode_activation(self):
        broken = copy.deepcopy(SPEC)
        broken["forward"]["PREPARED"].append("UNSPECIFIED")
        with self.assertRaisesRegex(MachineError, "phase graph"):
            validate_spec(broken)
        broken = copy.deepcopy(SPEC)
        broken["opcodes"]["HEDGE"]["phase"] = "PREPARED"
        with self.assertRaisesRegex(MachineError, "reserved opcode"):
            validate_spec(broken)

    def test_every_implemented_opcode_rejects_missing_required_fields(self):
        base = json.loads((ROOT / "cases" / "economic_program_demo.json").read_text())
        guarded = json.loads((ROOT / "cases" / "economic_program_guarded_demo.json").read_text())
        graph = json.loads((ROOT / "cases" / "economic_program_graph_demo.json").read_text())
        cases = {}
        for program in (base, guarded, graph):
            for index, ins in enumerate(program["instructions"]):
                cases.setdefault(ins["op"], (program, index))
        priced = copy.deepcopy(base)
        priced["instructions"][0] = {"op": "PRICE", "paths": ["market.source_a", "market.source_b"],
                                      "as": "coverage", "max_age_ms": 3600000,
                                      "max_dispersion_bps": 100}
        cases["PRICE"] = (priced, 0)
        for op in ("ESCALATE", "ABORT", "HOLD"):
            program = copy.deepcopy(graph)
            program["instructions"] = [{"op": op, "reason": "TEST"}]
            cases[op] = (program, 0)
        implemented = {name for name, rule in SPEC["opcodes"].items()
                       if rule["status"] == "implemented"}
        self.assertEqual(set(cases), implemented)
        for op, (program, index) in cases.items():
            compile_program(program)
            self.assertEqual(set(program["instructions"][index]),
                             set(SPEC["opcodes"][op]["fields"]))
            for field in SPEC["opcodes"][op]["fields"]:
                with self.subTest(opcode=op, field=field):
                    incomplete = copy.deepcopy(program)
                    del incomplete["instructions"][index][field]
                    with self.assertRaises(MachineError):
                        compile_program(incomplete)


if __name__ == "__main__":
    unittest.main()
