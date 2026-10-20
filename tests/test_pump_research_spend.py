import concurrent.futures
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from economic_machine.pump_research.research_spend import Spend,FOLDER,build_grant,authority,month
from economic_machine.pump_research.store import canonical
from test_pump_research_continuation import make_continuation_fixture

def setup_budget(base):
    make_continuation_fixture(base)
    folder=base/FOLDER;folder.mkdir()
    path=folder/'grant.json';path.write_text(canonical(build_grant(base))+'\n')
    path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'\n')
    return folder

class SpendTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.base=Path(t.name)
        self.folder=setup_budget(self.base);self.s=Spend(self.base);self.addCleanup(self.s.close)
    def test_latest_krw_ceiling_replaces_usd(self):
        self.assertEqual(self.s.grant['monthly_limit'],1000000)
        self.assertTrue(self.s.grant['replaces_earlier_usd1500_budget'])
    def test_grant_tamper_rejected(self):
        p=self.folder/'grant.json';v=json.loads(p.read_bytes());v['monthly_limit']=1500000;p.write_text(canonical(v))
        p.with_suffix('.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):authority(self.base)
    def test_unknown_upload_holds_storage_and_attempt(self):
        self.s.reserve('upload','uncertain',bytes_=8*1024**2,digest='a'*64)
        t=self.s.totals();self.assertEqual(t['cloud_reserved_bytes'],8*1024**2)
        self.assertGreater(t['operations_and_models_krw'],0)
        self.assertEqual(self.s.db.execute('SELECT verified FROM objects').fetchone()[0],0)
    def test_retry_charged_again_but_storage_not_duplicated(self):
        for key in ('first','retry'):self.s.reserve('upload',key,bytes_=1024,digest='b'*64)
        self.assertEqual(self.s.totals()['cloud_reserved_bytes'],1024)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM operations').fetchone()[0],2)
    def test_identity_collision_rejected(self):
        self.s.reserve('upload','a',bytes_=100,digest='a'*64)
        with self.assertRaisesRegex(ValueError,'SIZE'):self.s.reserve('upload','b',bytes_=101,digest='a'*64)
    def test_model_reserve_and_actual_are_not_double_counted(self):
        self.s.reserve('model','call',usd='0.5');self.assertEqual(self.s.totals()['operations_and_models_krw'],1000)
        self.s.model_finish('call','0.01');self.assertEqual(self.s.totals()['operations_and_models_krw'],20)
    def test_unknown_model_cost_keeps_reservation(self):
        self.s.reserve('model','call',usd='1');self.s.model_finish('call',None)
        self.assertEqual(self.s.totals()['operations_and_models_krw'],2000)
    def test_aggregate_model_and_storage_limit(self):
        self.s.reserve('model','near',usd='399.995')
        with self.assertRaisesRegex(ValueError,'MONTHLY'):self.s.reserve('read','over',bytes_=64*1024**2)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM operations').fetchone()[0],1)
    def test_persistent_storage_reserved_in_next_month(self):
        self.s.reserve('upload','a',bytes_=64*1024**2,digest='a'*64)
        with patch('economic_machine.pump_research.research_spend.month',return_value='2099-01'):
            self.assertGreater(self.s.totals()['storage_full_month_reserve_krw'],0)
            self.assertEqual(self.s.totals()['operations_and_models_krw'],0)
    def test_concurrent_aggregate_admission(self):
        self.s.baseline_models();self.s.reserve('model','near',usd='399.5')
        def admit(i):
            s=Spend(self.base)
            try:
                s.reserve('model',str(i),usd='0.4');return True
            except ValueError:return False
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(admit,range(4)))
        self.assertEqual(sum(results),1)
    def test_prior_model_ledger_untouched_and_reserved(self):
        path=self.base/'evidence/wallet-map-v1/model.sqlite3';db=sqlite3.connect(path)
        db.execute('CREATE TABLE calls(request_hash,reserved,actual,created_ms)')
        db.execute('INSERT INTO calls VALUES(?,?,?,?)',('old','0.1',None,time.time_ns()//1000000));db.commit();db.close()
        before=path.read_bytes();self.s.baseline_models();self.assertEqual(self.s.totals()['prior_model_reserve_krw'],200)
        self.assertEqual(path.read_bytes(),before)
    def test_kst_boundary(self):
        self.assertEqual(month(1793458800),'2026-11')
    def test_invalid_costs_and_sizes(self):
        for v in ('NaN','Infinity','-1'):
            with self.assertRaises(ValueError):self.s.reserve('model',v,usd=v)
        with self.assertRaises(ValueError):self.s.reserve('read','big',bytes_=64*1024**2+1)

if __name__=='__main__':unittest.main()
