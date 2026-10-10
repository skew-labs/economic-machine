import asyncio
import copy
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from machine_commerce.portal import create_portal
from machine_commerce.token_market import TokenMarket, normalize_pair, normalize_history, number, deployment

TOKEN='0x'+'a'*40
POOL='0x'+'b'*40
NOW=1791110000
PAIR={'pairs':[{'chainId':'arbitrum','pairAddress':POOL,'baseToken':{'address':TOKEN},'priceUsd':'0.01','volume':{'h24':12},'fdv':1600,'liquidity':{'usd':200}}]}
HISTORY={'data':{'attributes':{'ohlcv_list':[[NOW-3600,'.01','.02','.01','.012','10'],[NOW-7200,'.01','.01','.01','.01',0]]}}}

class TokenMarketTests(unittest.TestCase):
    def test_packaged_mainnet_identity_without_an_invented_price(self):
        env={k:v for k,v in os.environ.items() if k not in {'MACHINE_SKEW_TOKEN_ADDRESS','MACHINE_SKEW_POOL_ADDRESS'}}
        with patch.dict(os.environ,env,clear=True):
            result=asyncio.run(TokenMarket().market_snapshot())
        self.assertEqual(result['status'],'AWAITING_MARKET')
        self.assertEqual(result['token_address'],'0x6cee6a99af671900001469a41da46e2921d18678')
        self.assertEqual(result['deployment']['status'],'MAINNET_DEPLOYED_FINALIZED')
        self.assertEqual(result['deployment']['verification']['total_supply_at_state_block'],'0')
        self.assertIsNone(result['price_usd']);self.assertIsNone(result['market_cap_usd'])

    def test_unrelated_configured_token_never_inherits_deployment(self):
        with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':TOKEN,'MACHINE_SKEW_POOL_ADDRESS':''}):
            result=asyncio.run(TokenMarket().snapshot())
        self.assertIsNone(result['deployment'])

    def test_no_market_is_not_a_zero_price_or_fdv_market_cap(self):
        result=normalize_pair(PAIR,TOKEN,POOL,NOW)
        self.assertIsNone(result['market_cap_usd'])
        self.assertEqual(result['price_usd'],'0.01')
        self.assertEqual(result['volume_24h_usd'],'12')

    def test_chain_address_and_base_must_match_not_ticker(self):
        for key,value in [('chainId','ethereum'),('pairAddress','0x'+'c'*40),('baseToken',{'address':POOL,'symbol':'SKEW'})]:
            raw=copy.deepcopy(PAIR);raw['pairs'][0][key]=value
            with self.assertRaises(ValueError):normalize_pair(raw,TOKEN,POOL,NOW)

    def test_no_nonfinite_negative_boolean_or_zero_price(self):
        for value in ['NaN','Infinity','-1',True,'1e100']:
            self.assertIsNone(number(value))
        for value in ['0',None,False]:
            raw=copy.deepcopy(PAIR);raw['pairs'][0]['priceUsd']=value
            with self.assertRaises(ValueError):normalize_pair(raw,TOKEN,POOL,NOW)

    def test_history_is_sorted_deduplicated_and_not_interpolated(self):
        result=normalize_history(HISTORY,NOW)
        self.assertEqual([p['timestamp'] for p in result],[NOW-7200,NOW-3600])
        bad=copy.deepcopy(HISTORY);bad['data']['attributes']['ohlcv_list'][0][0]=NOW+1
        with self.assertRaises(ValueError):normalize_history(bad,NOW)
        bad=copy.deepcopy(HISTORY);bad['data']['attributes']['ohlcv_list']*=2
        with self.assertRaises(ValueError):normalize_history(bad,NOW)

    def test_unconfigured_does_not_contact_a_provider(self):
        async def forbidden(url):raise AssertionError('network call')
        with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':'','MACHINE_SKEW_POOL_ADDRESS':POOL}):
            result=asyncio.run(TokenMarket(forbidden).snapshot())
        self.assertEqual(result['status'],'AWAITING_TOKEN_ADDRESS')
        self.assertIsNone(result['price_usd']);self.assertEqual(result['candles'],[])

    def test_missing_pool_and_malformed_configuration_are_closed(self):
        for token,pool,status in [(TOKEN,'','AWAITING_MARKET'),('https://evil.invalid',POOL,'AWAITING_TOKEN_ADDRESS')]:
            with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':token,'MACHINE_SKEW_POOL_ADDRESS':pool}):
                self.assertEqual(asyncio.run(TokenMarket().snapshot())['status'],status)

    def test_cache_singleflight_and_no_old_price_on_new_source_failure(self):
        calls=[];clock=[NOW]
        async def fetch(url):
            calls.append(url)
            if clock[0]>NOW:raise ValueError('bad response')
            return HISTORY if '/ohlcv/' in url else PAIR
        async def run():
            feed=TokenMarket(fetch,lambda:clock[0]);results=await asyncio.gather(*[feed.snapshot() for _ in range(8)])
            self.assertEqual(len(calls),2);self.assertEqual(results[0]['history_status'],'AVAILABLE')
            clock[0]+=61;failed=await feed.snapshot()
            self.assertEqual(failed['status'],'SOURCE_UNAVAILABLE');self.assertIsNone(failed['price_usd'])
        with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':TOKEN,'MACHINE_SKEW_POOL_ADDRESS':POOL}):asyncio.run(run())

    def test_history_failure_keeps_independent_spot_and_null_chart(self):
        async def fetch(url):
            if '/ohlcv/' in url:raise ValueError('bad history')
            return PAIR
        with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':TOKEN,'MACHINE_SKEW_POOL_ADDRESS':POOL}):
            result=asyncio.run(TokenMarket(fetch,lambda:NOW).snapshot())
        self.assertEqual(result['status'],'AVAILABLE');self.assertEqual(result['candles'],[])
        self.assertEqual(result['history_status'],'UNAVAILABLE')

    def test_public_routes_and_stylesheet_order(self):
        with patch.dict(os.environ,{'MACHINE_SKEW_TOKEN_ADDRESS':''}), TestClient(create_portal()) as client:
            response=client.get('/market/skew');self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()['price_usd']);self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(client.post('/market/skew',json={}).status_code,405)
            html=client.get('/console').text
            self.assertGreater(html.index('href="/commerce/workspace-visuals.css'),html.index('href="/commerce/assistant.css'))
            for asset in ['workspace-visuals.js','workspace-visuals.css','token-market.js','token-market.css','assets/skew-token.svg']:
                self.assertEqual(client.get('/'+asset).status_code,200,asset)
            self.assertEqual(client.get('/market/.env').status_code,404)
