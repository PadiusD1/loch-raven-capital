"""Offline adversarial tests for source retrieval, temporal fitting and collectors."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from firm import analysts, memory_archive, memory_rag, research_rag
from firm.research_provenance import validate_research, decision_time
from scripts.refresh_research import persist, price_records

BASE={'desk':'fundamental','tickers':['AAPL'],'source_url':'https://www.apple.com/newsroom/test',
      'published_at':'2026-09-01T12:00:00Z','observed_at':'2026-09-02T12:00:00Z',
      'asof':'2026-08-31T00:00:00Z','title':'Apple revenue cashflow quarterly results',
      'body':'Apple quarterly revenue cashflow financial statements with dated evidence.'}

class ResearchIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def save(self,rows):
        (self.root/'fixture.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    def test_legacy_engine_timestamp(self):
        self.assertEqual(decision_time('2026-10-09 15:30 UTC').isoformat(),'2026-10-09T15:30:00+00:00')
    def test_date_only_starts_new_york_day(self):
        self.assertEqual(decision_time('2026-09-02').hour,4)
    def test_intraday_release_not_visible_at_start_of_day(self):
        self.save([BASE]);self.assertEqual(research_rag.retrieve('Apple',asof='2026-09-02',root=self.root),[])
    def test_wrong_ticker_excluded(self):
        self.save([BASE]);self.assertEqual(research_rag.retrieve('Apple revenue',ticker='MSFT',root=self.root),[])
    def test_unknown_entity_is_not_global_company_evidence(self):
        self.save([{**BASE,'tickers':[]}]);self.assertEqual(research_rag.retrieve('Apple',ticker='MSFT',root=self.root),[])
    def test_future_append_cannot_change_retrieval_ranking(self):
        self.save([BASE]);first=research_rag.retrieve('revenue',asof='2026-09-03',root=self.root)
        future={**BASE,'title':'revenue '*30,'observed_at':'2027-01-01T00:00:00Z'}
        self.save([BASE,future]);second=research_rag.retrieve('revenue',asof='2026-09-03',root=self.root)
        self.assertEqual(first,second)
    def test_tampering_rejected(self):
        r=validate_research(BASE)
        with self.assertRaises(ValueError):validate_research({**r,'body':r['body']+' ALTERED'})
    def test_stale_is_flagged(self):
        self.save([{**BASE,'max_age_days':1}])
        self.assertTrue(research_rag.retrieve('Apple',asof='2026-09-10',root=self.root)[0]['record']['stale'])
    def test_unchanged_refresh_preserves_first_observation(self):
        self.assertEqual(persist([BASE],self.root),1)
        self.assertEqual(persist([{**BASE,'observed_at':'2026-10-01T00:00:00Z'}],self.root),0)
        self.assertEqual(len(research_rag.load_records(self.root)),1)
    def test_cache_not_just_number_of_records(self):
        self.save([BASE]);first=research_rag.retrieve('revenue',root=self.root)
        self.save([{**BASE,'body':'New revenue cashflow evidence changed substantially in the same record count.'}])
        second=research_rag.retrieve('revenue',root=self.root)
        self.assertNotEqual(first[0]['record']['content_sha256'],second[0]['record']['content_sha256'])
    def test_all_stopword_corpus_safe(self):
        self.save([{**BASE,'title':'the and or','body':'the and of the and the or of and of'}])
        self.assertEqual(research_rag.retrieve('xyzzy',root=self.root),[])
    def test_zero_k_no_results(self):
        self.save([BASE]);self.assertEqual(research_rag.retrieve('Apple',k=0,root=self.root),[])
    def test_undated_packet_does_not_assume_today(self):
        self.assertEqual(research_rag.desk_evidence('fundamental',{'ticker':'AAPL'},root=self.root),[])
    def test_desks_receive_legacy_format(self):
        self.save([BASE]);original=research_rag.desk_evidence
        with patch.object(research_rag,'desk_evidence',side_effect=lambda d,p:original(d,p,root=self.root)):
            p=analysts._desk_packet('fundamental',{'ticker':'AAPL','asof':'2026-09-03 15:00 UTC'})
        self.assertEqual(len(p['external_research']),1)
    def test_methodology_allowed_without_ticker(self):
        self.save([{**BASE,'desk':'technical','tickers':[],'scope':'methodology'}])
        self.assertEqual(len(research_rag.retrieve('revenue',desk='technical',ticker='MSFT',root=self.root)),1)
    def test_historical_index_fits_only_past_docs(self):
        memos=memory_archive.load_memos()[:20]
        pivot=memos[-1].asof
        if not any(m.asof<pivot for m in memos):pivot='2026-06-20'
        past=tuple(m for m in memos if m.asof<pivot)
        future=replace(memos[0],id='future-x',asof='2027-01-01',thesis='quantum hypernova '*30)
        a=memory_rag.MemoryIndex(past).search('momentum',asof=pivot)
        b=memory_rag.MemoryIndex(past+(future,)).search('momentum',asof=pivot)
        self.assertEqual([(h.memo.id,h.score) for h in a],[(h.memo.id,h.score) for h in b])
    def test_future_lessons_are_excluded(self):
        self.assertEqual(memory_rag.knowledge_block('conviction sizing',asof='2026-07-01')[1],[])
    def test_price_math_benchmark_identity(self):
        from datetime import date,timedelta
        rows={(date(2025,1,1)+timedelta(days=i)).isoformat():100+i for i in range(100)}
        records=price_records('SPY',rows,rows,'2026-10-10T00:00:00+00:00',
                              'https://query1.finance.yahoo.com/v8/finance/chart/SPY','a'*64)
        self.assertEqual(records[0]['metrics']['excess_vs_SPY_63d_pp'],0)
        self.assertAlmostEqual(records[1]['metrics']['beta_to_SPY'],1)
    def test_schema_title_type_rejected(self):
        with self.assertRaises(ValueError):validate_research({**BASE,'title':5})
    def test_source_url_credentials_rejected(self):
        with self.assertRaises(ValueError):validate_research({**BASE,'source_url':'https://a:b@example.com'})

class SourceWiringTests(unittest.TestCase):
    def test_all_five_prompts_contain_source_evidence(self):
        from datetime import datetime,timedelta
        corpus=research_rag.load_records()
        asof=(datetime.fromisoformat(max(r['observed_at'] for r in corpus))+timedelta(seconds=1)).isoformat()
        packet={'ticker':'AAPL','asof':asof}
        prefixes={'fundamental':'RF','technical':'RT','sentiment':'RS','macro':'RM','quant':'RQ'}
        for desk,prefix in prefixes.items():
            self.assertIn(prefix+'1',analysts._desk_prompt(desk,packet))
        for prefix in prefixes.values():
            self.assertIn(prefix+'1',analysts._panel_prompt(packet))
    def test_pm_has_the_same_external_citation_identifiers(self):
        from firm import debate,memory_guard
        import config
        from datetime import datetime,timedelta
        corpus=research_rag.load_records()
        asof=(datetime.fromisoformat(max(r['observed_at'] for r in corpus))+timedelta(seconds=1)).isoformat()
        with patch.object(config,'MEMORY_RAG_ENABLED',True):
            text=debate._precedent_note({'ticker':'AAPL','asof':asof},{})
        self.assertIn('[RF1]',text)
        ids=[r['citation'] for r in debate._MEM.last['external_sources']]
        self.assertEqual(memory_guard.check_citations('[RF1]',0,0,ids)['status'],'pass')
        self.assertEqual(memory_guard.check_citations('[RF99]',0,0,ids)['status'],'fail')
    def test_read_only_research_routes(self):
        from fastapi.testclient import TestClient
        import memory_console
        client=TestClient(memory_console.app)
        self.assertGreaterEqual(client.get('/api/memory/research/status').json()['records'],11)
        self.assertEqual(client.get('/api/memory/research/search',params={'q':'Apple','asof':'bad'}).status_code,400)
        hits=client.get('/api/memory/research/search',params={'q':'Apple revenue','desk':'fundamental','ticker':'AAPL'}).json()['hits']
        self.assertTrue(hits)

if __name__=='__main__':unittest.main()
