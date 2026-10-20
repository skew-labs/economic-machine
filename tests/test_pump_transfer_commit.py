"""Mock metadata regressions; no chain, provider, model, or wallet access."""
import struct
from types import SimpleNamespace as NS
import unittest

from economic_machine.pump_research.store import encode,decode
from economic_machine.pump_research.wire import extract,SYSTEM,TOKENS


def key(n):return encode(bytes([n])*32)
ROUTER=key(3)
NESTED=key(4)


def invoke(program,depth):return f'Program {program} invoke [{depth}]'
def success(program):return f'Program {program} success'
def failed(program):return f'Program {program} failed: custom program error: 0x1'
def instruction(program=2,depth=2,amount=100):
    return NS(program_id_index=program,accounts=[0,1],data=struct.pack('<IQ',2,amount),stack_height=depth)


class TransferCommitTests(unittest.TestCase):
    def extract(self,inners,logs,outer=None,failed_tx=False,keys=None):
        keys=keys or [key(1),key(2),SYSTEM,ROUTER,NESTED]
        meta=NS(loaded_writable_addresses=[],loaded_readonly_addresses=[],post_balances=[],
            pre_token_balances=[],post_token_balances=[],log_messages=logs,
            inner_instructions=[NS(index=0,instructions=inners)],HasField=lambda k:failed_tx if k=='err' else False)
        return extract(encode(b'x'*64),keys,outer or [(3,[],b'')],meta,250000000)

    def test_successful_cpi_has_exact_committed_order(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)])
        self.assertEqual([(t['ix'],t['amount']) for t in tx['transfers']],[(1,'100')])
        self.assertEqual(tx['instruction_commit_evidence']['committed_inner_orders'],[1])
        self.assertFalse(tx['instruction_commit_evidence']['full_transfer_coverage'])

    def test_caught_failed_transfer_is_not_committed(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),failed(SYSTEM),success(ROUTER)])
        self.assertEqual(tx['transfers'],[])
        self.assertIn('CPI_REVERTED_INVOCATION',tx['instruction_commit_evidence']['errors'])

    def test_successful_child_of_reverted_subtree_is_not_committed(self):
        branch=NS(program_id_index=4,accounts=[],data=b'',stack_height=2)
        tx=self.extract([branch,instruction(depth=3)],[invoke(ROUTER,1),invoke(NESTED,2),
            invoke(SYSTEM,3),success(SYSTEM),failed(NESTED),success(ROUTER)])
        self.assertEqual(tx['transfers'],[])
        self.assertEqual(tx['instruction_commit_evidence']['omitted_inner_count'],2)

    def test_failed_sibling_does_not_erase_successful_sibling(self):
        tx=self.extract([instruction(amount=100),instruction(amount=200)],
            [invoke(ROUTER,1),invoke(SYSTEM,2),failed(SYSTEM),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)])
        self.assertEqual([(t['ix'],t['amount']) for t in tx['transfers']],[(2,'200')])

    def test_missing_logs_omit_cpi_but_keep_successful_top_level_transfer(self):
        tx=self.extract([instruction()],[],outer=[(3,[],b''),(2,[0,1],struct.pack('<IQ',2,300))])
        self.assertEqual([(t['ix'],t['amount']) for t in tx['transfers']],[(65536,'300')])
        self.assertEqual(tx['instruction_commit_evidence']['omitted_inner_count'],1)

    def test_truncated_stack_omits_suspect_cpi(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM)])
        self.assertEqual(tx['transfers'],[])
        self.assertIn('CPI_INCOMPLETE_INVOCATION_STACK',tx['instruction_commit_evidence']['errors'])

    def test_log_program_mismatch_omits_cpi(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(NESTED,2),success(NESTED),success(ROUTER)])
        self.assertEqual(tx['transfers'],[])
        self.assertIn('CPI_INSTRUCTION_LOG_MISMATCH',tx['instruction_commit_evidence']['errors'])

    def test_stack_height_metadata_mismatch_omits_cpi(self):
        tx=self.extract([instruction(depth=3)],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)])
        self.assertEqual(tx['transfers'],[])

    def test_legacy_missing_stack_height_can_use_complete_unambiguous_logs(self):
        ix=instruction();del ix.stack_height
        tx=self.extract([ix],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)])
        self.assertEqual(len(tx['transfers']),1)

    def test_missing_recorded_invocation_omits_group(self):
        tx=self.extract([instruction(),instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)])
        self.assertEqual(tx['transfers'],[])
        self.assertIn('CPI_INSTRUCTION_LOG_CARDINALITY',tx['instruction_commit_evidence']['errors'])

    def test_ambiguous_repeated_outer_program_omits_cpi(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)],
            outer=[(3,[],b''),(3,[],b'')])
        self.assertEqual(tx['transfers'],[])
        self.assertIn('CPI_AMBIGUOUS_OUTER_LOG_MAPPING',tx['instruction_commit_evidence']['errors'])

    def test_repeated_outer_program_with_complete_roots_is_unambiguous(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER),
            invoke(ROUTER,1),success(ROUTER)],outer=[(3,[],b''),(3,[],b'')])
        self.assertEqual(len(tx['transfers']),1)

    def test_logless_distinct_outer_instruction_does_not_shift_cpi_parent(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)],
            outer=[(3,[],b''),(4,[],b'')])
        self.assertEqual(len(tx['transfers']),1)

    def test_user_logged_runtime_like_text_is_not_commit_proof(self):
        tx=self.extract([instruction()],[f'Program log: {line}' for line in
            [invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)]])
        self.assertEqual(tx['transfers'],[])

    def test_failed_transaction_never_exposes_instruction_transfers(self):
        tx=self.extract([instruction()],[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)],failed_tx=True)
        self.assertEqual(tx['transfers'],[])

    def test_legacy_token_and_token2022_checked_cpi_share_commit_guard(self):
        for program in TOKENS:
            keys=[key(1),key(2),program,ROUTER,key(5),key(6)]
            ix=NS(program_id_index=2,accounts=[0,4,1,5],data=b'\x0c'+struct.pack('<Q',100)+b'\x06',stack_height=2)
            for state,expected in [(success,1),(failed,0)]:
                tx=self.extract([ix],[invoke(ROUTER,1),invoke(program,2),state(program),success(ROUTER)],keys=keys)
                self.assertEqual(len(tx['transfers']),expected)

    def test_loaded_program_address_participates_in_commit_mapping(self):
        ix=instruction(program=3)
        meta=NS(loaded_writable_addresses=[decode(SYSTEM)],loaded_readonly_addresses=[],post_balances=[],
            pre_token_balances=[],post_token_balances=[],inner_instructions=[NS(index=0,instructions=[ix])],
            log_messages=[invoke(ROUTER,1),invoke(SYSTEM,2),success(SYSTEM),success(ROUTER)],HasField=lambda k:False)
        tx=extract(encode(b'x'*64),[key(1),key(2),ROUTER],[(2,[],b'')],meta,250000000)
        self.assertEqual(len(tx['transfers']),1)


if __name__=='__main__':unittest.main()
