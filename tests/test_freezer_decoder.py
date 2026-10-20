from historical_scope import HistoricalScopeTestCase
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economic_machine.arbitrum_research import freezer_decoder as d


class DecoderTests(HistoricalScopeTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.root=self.base/'evidence/freezer-continuous-v1'; self.root.mkdir(parents=True)
        (self.root/'policy.json').write_text('{"original":true}')
        self.old=self.base/'bin/freezer-decode'; self.old.parent.mkdir(); self.old.write_bytes(b'legacy')
        self.new=self.base/d.TYPED_RELATIVE; self.new.write_bytes(b'typed')
        self.sha=d.digest(self.new); self.patch=patch.object(d,'TYPED_SHA',self.sha); self.patch.start(); self.addCleanup(self.patch.stop)
        src=self.base/'source/native/freezer_decode_typed_v1/main.go'; src.parent.mkdir(parents=True); src.write_bytes(b'pinned source')
        self.value=dict(schema='economic-machine.freezer-decoder-amendment.v1',
            original_policy_sha256=d.digest(self.root/'policy.json'), deadline_ms=d.DEADLINE,
            monthly_budget_krw=1000000, minimum_host_free_bytes=128*1024**3, batch_headroom_bytes=8*1024**3,
            source_writers=1, consumers=3, max_inflight=4, max_native_input_bytes=60*1024**2,
            binary_relative=d.TYPED_RELATIVE,binary_sha256=self.sha,environment=d.NATIVE_ENV,
            reset_cursor=False,preserve_existing_policies=True,first_batch=37099136,authorized_at_ms=1,
            native_sources={'native/freezer_decode_typed_v1/main.go':d.digest(src)})
        self.path=self.root/'decoder-v1.json'; self.save()
        self.folder=self.root/'batch-37099136-37164671'
    def save(self):
        self.path.write_text(json.dumps(self.value)); self.route=dict(path=str(self.path),sha256=d.digest(self.path),value=self.value)
    def check(self,**kwargs):return d.amendment(self.base,self.path,self.route['sha256'],**kwargs)
    def profile(self):
        d.select_batch(self.base,self.folder,self.old,37099136,self.route)
        page=self.folder/'window-37099136-37115519';page.mkdir(exist_ok=True)
        return d.page_profile(self.base,page,self.new,42161,37099136,True)
    def report(self):
        profile=self.profile()
        return dict(source_proof={'source':{'binary_sha256':self.sha,'decoder_execution':profile}},
            derived_reconstruction={'decoder_execution':profile})
    def test_original_policy_and_binary_unchanged(self):
        original=(self.root/'policy.json').read_bytes();self.check()
        self.assertEqual((self.root/'policy.json').read_bytes(),original);self.assertEqual(self.old.read_bytes(),b'legacy')
    def test_new_only_after_boundary(self):
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099135,self.route),self.old)
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,self.route),self.new)
        self.assertTrue((self.folder/'decoder-selection.json').exists())
    def test_disabled_new_write_uses_old(self):
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,None),self.old)
    def test_old_pending_policy_stays_old(self):
        self.folder.mkdir();(self.folder/'policy.json').write_text(json.dumps({'binary_sha256':d.digest(self.old)}))
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,self.route),self.old)
        self.assertFalse((self.folder/'decoder-selection.json').exists())
    def test_typed_resume_independent_of_future_flag(self):
        self.profile();(self.folder/'policy.json').write_text(json.dumps({'binary_sha256':self.sha}))
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,None),self.new)
    def test_selection_survives_crash_before_batch_policy(self):
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,self.route),self.new)
        self.assertFalse((self.folder/'policy.json').exists())
        self.assertEqual(d.select_batch(self.base,self.folder,self.old,37099136,None),self.new)
    def test_unknown_binary_not_admitted(self):
        self.folder.mkdir();(self.folder/'policy.json').write_text(json.dumps({'binary_sha256':'a'*64}))
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):d.select_batch(self.base,self.folder,self.old,37099136,self.route)
    def test_pending_prepared_without_policy_rejected(self):
        p=self.folder/'window-1-2';p.mkdir(parents=True);(p/'acquired.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'POLICY_MISSING'):d.select_batch(self.base,self.folder,self.old,37099136,self.route)
    def test_scope_never_expands(self):
        for k,v in [('deadline_ms',d.DEADLINE+1),('monthly_budget_krw',2000000),('consumers',6),('minimum_host_free_bytes',1),('max_inflight',8),('max_native_input_bytes',64*1024**2),('reset_cursor',True)]:
            original=self.value[k];self.value[k]=v;self.save()
            with self.subTest(key=k),self.assertRaisesRegex(ValueError,'AMENDMENT_SCOPE'):self.check()
            self.value[k]=original
    def test_changed_binary_fails(self):
        self.new.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'BINARY_CHANGED'):self.check()
    def test_changed_source_fails(self):
        (self.base/'source/native/freezer_decode_typed_v1/main.go').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):self.check()
    def test_changed_original_policy_fails(self):
        (self.root/'policy.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'AMENDMENT_SCOPE'):self.check()
    def test_wrong_hash_fails(self):
        with self.assertRaisesRegex(ValueError,'AMENDMENT_HASH'):d.amendment(self.base,self.path,'a'*64)
    def test_path_escape_and_symlink_fail(self):
        self.value['native_sources']={'native/freezer_decode_typed_v1/../other':'a'*64};self.save()
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):self.check()
        self.save();self.path.unlink();self.path.symlink_to(self.root/'policy.json')
        with self.assertRaisesRegex(ValueError,'AMENDMENT_HASH'):self.check()
    def test_deadline_blocks_admission_not_readonly_replay(self):
        with patch.object(d.time,'time_ns',return_value=(d.DEADLINE+1)*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):self.check(admission=True)
            self.check(admission=False)
    def test_page_chain_prepared_boundary(self):
        self.profile()
        for chain,first,prepared in [(4663,37099136,True),(42161,1,True),(42161,37099136,False)]:
            with self.subTest(chain=chain,first=first,prepared=prepared),self.assertRaisesRegex(ValueError,'PAGE_SCOPE'):
                d.page_profile(self.base,self.folder/'window-x',self.new,chain,first,prepared)
    def test_page_profile_cannot_downgrade(self):
        profile=self.profile()
        with self.assertRaisesRegex(ValueError,'PAGE_DOWNGRADE'):
            d.page_profile(self.base,self.folder/'window-x',self.old,42161,37099136,True,{'decoder_execution':profile})
    def test_native_controls_only_subprocess_environment(self):
        before=os.environ.copy();env=d.native_environment(self.profile())
        self.assertEqual({k:env[k] for k in d.NATIVE_ENV},d.NATIVE_ENV);self.assertEqual(dict(os.environ),before)
    def test_typed_replay_uses_retained_profile_without_env(self):
        report=self.report()
        with patch.dict(os.environ,{},clear=True):
            binary,profile=d.replay_binary(self.base,report)
        self.assertEqual(binary,self.new);self.assertEqual(profile,report['source_proof']['source']['decoder_execution'])
    def test_profile_tamper_or_missing_reconstruction_fails(self):
        report=self.report()
        for mutate in ['environment','path','derived','proofsha']:
            r=copy.deepcopy(report)
            if mutate=='environment':r['source_proof']['source']['decoder_execution']['environment']['GOMAXPROCS']='16'
            if mutate=='path':r['source_proof']['source']['decoder_execution']['binary_relative']='bin/unknown'
            if mutate=='derived':r['derived_reconstruction']={}
            if mutate=='proofsha':r['source_proof']['source']['binary_sha256']='a'*64
            with self.subTest(mutate=mutate),self.assertRaisesRegex(ValueError,'REPLAY_PROFILE'):d.replay_binary(self.base,r)
    def test_legacy_replay_stays_original(self):
        report={'source_proof':{'source':{'binary_sha256':d.digest(self.old)}}}
        self.assertEqual(d.replay_binary(self.base,report),(self.old,None))
    def test_new_sha_cannot_masquerade_as_legacy(self):
        report={'source_proof':{'source':{'binary_sha256':self.sha}}}
        with self.assertRaisesRegex(ValueError,'BINARY_CHANGED'):d.replay_binary(self.base,report)
    def test_incomplete_config_fails(self):
        with patch.dict(os.environ,{'EM_FREEZER_DECODER_AMENDMENT':str(self.path)},clear=True):
            with self.assertRaisesRegex(ValueError,'ARGUMENT'):d.configured(self.base)


if __name__=='__main__':unittest.main()
