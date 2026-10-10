"""No-network tests for free-source budgets, provenance, universes and runtime wiring."""
import json
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import config
from firm import research_automation as auto, research_rag
from firm.research_transport import SourceClient, BudgetExceeded, MAX_BYTES
from firm.research_provenance import validate_research
from scripts import refresh_research as feeds
from scripts.free_source_collection import bls_record, fed_records, reconciliation

NOW=datetime(2026,10,10,3,tzinfo=timezone.utc)
BASE={'desk':'fundamental','source_url':'https://data.sec.gov/example','tickers':['AAPL'],
      'published_at':'2026-09-01T00:00:00+00:00','observed_at':'2026-09-02T00:00:00+00:00',
      'asof':'2026-08-31T00:00:00+00:00','title':'Apple revenue facts',
      'body':'Apple revenue and historical financial statement data.'}

class FreeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.now=NOW;self.tick=1.
    def sleep(self,n):self.tick+=n
    def client(self,handler,**kwargs):
        c=SourceClient(self.root,clock=lambda:self.now,monotonic=lambda:self.tick,sleep=self.sleep,
                       transport=httpx.MockTransport(handler),**kwargs)
        self.addCleanup(c.http.close);return c
    def test_unapproved_host_no_request(self):
        c=self.client(lambda r:self.fail('network'))
        for url in ['https://evil.test','http://data.sec.gov/a','https://user:pass@data.sec.gov/a','https://data.sec.gov:444/a']:
            with self.assertRaises(ValueError):c.get(url)
        self.assertEqual(c.counts['requests'],0)
    def test_cache_preserves_observation(self):
        c=self.client(lambda r:httpx.Response(200,json={'value':42}))
        a=c.get('https://data.sec.gov/test');self.now+=timedelta(minutes=1)
        b=c.get('https://data.sec.gov/test')
        self.assertEqual(a.extensions['research_observed_at'],b.extensions['research_observed_at'])
        self.assertEqual(c.counts['requests'],1);self.assertEqual(c.counts['cache_hits'],1)
    def test_etag_304_preserves_observed_but_updates_checked(self):
        calls=[]
        def handler(r):
            calls.append(r)
            return httpx.Response(200,json={'v':1},headers={'ETag':'x'}) if len(calls)==1 else httpx.Response(304)
        c=self.client(handler);a=c.get('https://data.sec.gov/test')
        self.now+=timedelta(days=1);b=c.get('https://data.sec.gov/test')
        self.assertEqual(calls[-1].headers['if-none-match'],'x')
        self.assertEqual(a.extensions['research_observed_at'],b.extensions['research_observed_at'])
        self.assertNotEqual(a.extensions['research_checked_at'],b.extensions['research_checked_at'])
    def test_request_budget_hard_stop(self):
        c=self.client(lambda r:httpx.Response(200,text='ok'),max_requests=1)
        c.get('https://data.sec.gov/one')
        with self.assertRaises(BudgetExceeded):c.get('https://data.sec.gov/two')
    def test_retry_after_respected(self):
        calls=[]
        def handler(r):
            calls.append(self.tick)
            return httpx.Response(429,headers={'Retry-After':'4'}) if len(calls)==1 else httpx.Response(200,text='ok')
        c=self.client(handler);c.get('https://data.sec.gov/test')
        self.assertGreaterEqual(calls[1]-calls[0],4)
    def test_long_retry_does_not_hammer(self):
        c=self.client(lambda r:httpx.Response(429,headers={'Retry-After':'3600'}))
        with self.assertRaises(BudgetExceeded):c.get('https://data.sec.gov/test')
        self.assertEqual(c.counts['requests'],1)
    def test_redirect_rejected(self):
        c=self.client(lambda r:httpx.Response(302,headers={'Location':'http://localhost/'}))
        with self.assertRaises(ValueError):c.get('https://data.sec.gov/test')
    def test_byte_limit(self):
        c=self.client(lambda r:httpx.Response(200,content=b'a'*(MAX_BYTES+1)))
        with self.assertRaises(ValueError):c.get('https://data.sec.gov/test')
    def test_corrupted_cache_reloaded(self):
        c=self.client(lambda r:httpx.Response(200,text='healthy'))
        c.get('https://data.sec.gov/test')
        next(self.root.glob('*.body')).write_text('bad')
        self.assertEqual(c.get('https://data.sec.gov/test').text,'healthy')
        self.assertEqual(c.counts['requests'],2)
    def test_failures_do_not_refresh_cache(self):
        c=self.client(lambda r:httpx.Response(200,text='healthy'));c.get('https://data.sec.gov/test')
        meta=next(self.root.glob('*.json'));original=meta.read_text()
        self.now+=timedelta(days=1)
        c.http.close();c.http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
        with self.assertRaises(httpx.HTTPStatusError):c.get('https://data.sec.gov/test')
        self.assertEqual(meta.read_text(),original)

class AutomationQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def test_holdings_options_prioritized_and_private_state_not_mutated(self):
        p=self.root/'sleeve_calls_only.json';p.write_text(json.dumps({'positions':{'LONG_OPTION_SYMBOL':{'underlying':'ZZZZ'}}}))
        original=p.read_text()
        with patch.object(config,'STATE_DIR',self.root):
            plan=auto.universe_plan(['AAAA'],include_state=True,when=NOW)
        self.assertEqual(plan['priority'],['ZZZZ','AAAA']);self.assertEqual(p.read_text(),original)
        self.assertIn('ZZZZ',plan['sec']);self.assertIn('ZZZZ',plan['prices'])
    def test_public_universe_does_not_read_local_book(self):
        (self.root/'sleeve_alpha.json').write_text(json.dumps({'positions':{'ZZZZ':{}}}))
        with patch.object(config,'STATE_DIR',self.root):p=auto.universe_plan(when=NOW)
        self.assertNotIn('ZZZZ',p['prices'])
    def test_all_rotation_core_and_cash_etfs_covered(self):
        p=auto.universe_plan(when=NOW)
        self.assertTrue(set(config.ROTATION_UNIVERSE+[config.CASH_ETF,config.BENCHMARK])<=set(p['prices']))
        self.assertLessEqual(len(p['prices']),64);self.assertLessEqual(len(p['sec']),24)
    def test_exploration_rotates(self):
        a=auto.universe_plan(when=NOW);b=auto.universe_plan(when=NOW+timedelta(days=1))
        self.assertNotEqual(a['sec'],b['sec'])
    def test_bad_symbols_rejected(self):
        self.assertEqual(auto.symbols(['../etc','AAPL','MSFT;exit','aapl',None]),['AAPL'])
    def test_os_lock_prevents_second_writer(self):
        with auto.collection_lock(self.root) as first:
            with auto.collection_lock(self.root) as second:self.assertTrue(first);self.assertFalse(second)
        with auto.collection_lock(self.root) as third:self.assertTrue(third)
    def test_A_B_A_revision_preserved(self):
        for i,text in enumerate(['A'*30,'B'*30,'A'*30]):
            feeds.persist([{**BASE,'body':text,'observed_at':f'2026-09-0{2+i}T00:00:00+00:00'}],self.root)
        self.assertEqual(len(research_rag.load_records(self.root)),3)
    def test_corrupt_row_does_not_hide_healthy_next_row(self):
        (self.root/'test.jsonl').write_text('BAD\n'+json.dumps(BASE)+'\n')
        self.assertEqual(len(research_rag.load_records(self.root)),1)
        self.assertEqual(research_rag.corpus_status(self.root)['rejected_rows'],1)
    def test_nonfinite_metadata_rejected(self):
        for v in [float('nan'),float('inf'),True,-1,'zero']:
            with self.assertRaises(ValueError):validate_research({**BASE,'max_age_days':v})
    def test_global_policy_source_reaches_sentiment(self):
        (self.root/'test.jsonl').write_text(json.dumps({**BASE,'desk':'sentiment','tickers':[],'scope':'market'})+'\n')
        self.assertTrue(research_rag.retrieve('revenue',ticker='MSFT',desk='sentiment',root=self.root))
    def test_health_distinguishes_method_from_company_evidence(self):
        (self.root/'test.jsonl').write_text(json.dumps({**BASE,'scope':'methodology','tickers':[]})+'\n')
        h=auto.health(self.root,asof=NOW.isoformat(),wanted=['AAPL'])
        self.assertEqual(h['desks']['fundamental']['methodology'],1)
        self.assertIn('fundamental',h['missing_or_stale_company_evidence']['AAPL'])
    def test_runtime_disabled_is_no_network(self):
        with patch.object(config,'RESEARCH_AUTO_REFRESH',False),patch.object(subprocess_module(),'run',side_effect=AssertionError):
            self.assertEqual(auto.prepare_cycle(['AAPL'])['status'],'disabled')
    def test_runtime_calls_only_collector_with_timeout(self):
        def child(*args,**kwargs):
            (self.root/'research_refresh_report.json').write_text(json.dumps({
                'started_at':datetime.now(timezone.utc).isoformat(),'status':'healthy','errors':[],'skipped':[]}))
            return SimpleNamespace(returncode=0)
        with patch.object(config,'RESEARCH_AUTO_REFRESH',True),patch.object(config,'STATE_DIR',self.root),\
             patch.object(auto,'health',return_value={}),patch.object(auto.subprocess,'run',side_effect=child) as run:
            self.assertEqual(auto.prepare_cycle(['AAPL'])['status'],'refreshed')
            self.assertIn('scripts.refresh_research',run.call_args.args[0]);self.assertEqual(run.call_args.kwargs['timeout'],105)
            self.assertNotIn('run_cycle.py',' '.join(run.call_args.args[0]))
            auto.prepare_cycle(['AAPL']);self.assertEqual(run.call_count,1)
    def test_partial_zero_exit_is_still_degraded(self):
        def child(*args,**kwargs):
            (self.root/'research_refresh_report.json').write_text(json.dumps({
                'started_at':datetime.now(timezone.utc).isoformat(),'status':'degraded',
                'errors':[{'source':'BLS'}],'skipped':[]}))
            return SimpleNamespace(returncode=0)
        with patch.object(config,'RESEARCH_AUTO_REFRESH',True),patch.object(config,'STATE_DIR',self.root),             patch.object(auto,'health',return_value={}),patch.object(auto.subprocess,'run',side_effect=child):
            result=auto.prepare_cycle(['AAPL'])
        self.assertEqual(result['status'],'degraded');self.assertEqual(result['source_errors'],1)
        self.assertFalse(json.loads((self.root/'research_cycle_refresh.json').read_text())['ok'])
    def test_quality_warning_change_is_preserved(self):
        feeds.persist([BASE],self.root)
        changed={**BASE,'observed_at':'2026-09-03T00:00:00+00:00','quality_warnings':['Mirror mismatch']}
        self.assertEqual(feeds.persist([changed],self.root),1)
        self.assertEqual(feeds.persist([changed],self.root),0)
        self.assertEqual(len(research_rag.load_records(self.root)),2)
    def test_future_source_health_excludes(self):
        feeds.persist([BASE],self.root)
        self.assertEqual(auto.health(self.root,asof='2020-01-01')['latest_topics'],0)

class FeedAdapterTests(unittest.TestCase):
    def response(self,obj,content=False):
        return httpx.Response(200,**({'content':obj} if content else {'json':obj}),
            request=httpx.Request('GET','https://data.sec.gov/test'),
            extensions={'research_observed_at':NOW.isoformat()})
    def test_bls_rejects_wrong_series(self):
        c=SimpleNamespace(get=lambda url:self.response({'status':'REQUEST_SUCCEEDED','Results':{'series':[{'seriesID':'WRONG'}]}}))
        with self.assertRaises(ValueError):bls_record(c,'LNS14000000')
    def test_bls_excludes_annual_and_future_keeps_footnotes(self):
        rows=[{'year':'2026','period':'M09','value':'4.1','footnotes':[{'text':'Preliminary'}]},
              {'year':'2026','period':'M13','value':'9'},{'year':'2027','period':'M01','value':'8'}]
        c=SimpleNamespace(get=lambda url:self.response({'status':'REQUEST_SUCCEEDED','Results':{'series':[{'seriesID':'LNS14000000','data':rows}]}}))
        r=bls_record(c,'LNS14000000');self.assertEqual(len(r['observations']),1)
        self.assertEqual(r['observations'][0]['footnotes'][0]['text'],'Preliminary')
    def test_bls_future_missing_value_does_not_drop_valid_series(self):
        rows=[{'year':'2026','period':'M09','value':'4.1'},
              {'year':'2027','period':'M01','value':'not published'}]
        c=SimpleNamespace(get=lambda url:self.response({'status':'REQUEST_SUCCEEDED',
              'Results':{'series':[{'seriesID':'LNS14000000','data':rows}]}}))
        self.assertEqual(bls_record(c,'LNS14000000')['observations'][0]['value'],4.1)
    def test_bls_explicit_missing_value_is_not_zero(self):
        rows=[{'year':'2026','period':'M09','value':'-'},
              {'year':'2026','period':'M08','value':'4.1'}]
        c=SimpleNamespace(get=lambda url:self.response({'status':'REQUEST_SUCCEEDED',
              'Results':{'series':[{'seriesID':'LNS14000000','data':rows}]}}))
        values=[r['value'] for r in bls_record(c,'LNS14000000')['observations']]
        self.assertEqual(values,[4.1])
    def test_mirror_disagreement_not_independent(self):
        rows=[{'topic_id':'bls:LNS14000000','observations':[{'date':'2026-09-01','value':4.1}]},
              {'topic_id':'fred:UNRATE','observations':[{'date':'2026-09-01','value':4.8}]}]
        r=reconciliation(rows)[0];self.assertFalse(r['independent_sources']);self.assertEqual(r['status'],'vintage_or_value_mismatch')
    def test_fed_rejects_entity_xml(self):
        c=SimpleNamespace(get=lambda url:self.response(b'<!DOCTYPE a><rss/>',True))
        with self.assertRaises(ValueError):fed_records(c,'https://www.federalreserve.gov/feeds/speeches.xml')
    def test_fed_dated_primary_feed_routes_two_desks(self):
        xml=b'<rss><channel><item><title>Monetary policy statement</title><link>https://www.federalreserve.gov/newsevents/test.htm</link><pubDate>Fri, 09 Oct 2026 18:00:00 GMT</pubDate><description>Official policy news.</description></item></channel></rss>'
        c=SimpleNamespace(get=lambda url:self.response(xml,True))
        rows=fed_records(c,'https://www.federalreserve.gov/feeds/press_monetary.xml')
        self.assertEqual({r['desk'] for r in rows},{'macro','sentiment'})
        self.assertEqual(rows[0]['lineage_group'],rows[1]['lineage_group'])
    def test_sec_share_units_not_converted_to_USD(self):
        from scripts.fetch_sec_research import extract_facts
        f={'filed':'2026-08-01','form':'10-Q','end':'2026-06-30','val':120}
        obj={'facts':{'us-gaap':{'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[f],'USD':[{**f,'val':999}]}}}}}
        rows=extract_facts(obj,observed_at=NOW.isoformat());self.assertEqual(len(rows),1);self.assertEqual(rows[0]['unit'],'shares')

def subprocess_module():return auto.subprocess

if __name__=='__main__':unittest.main()
