from pathlib import Path
root=Path.cwd()
p=root/'firm/research_automation.py';s=p.read_text()
s=s.replace("            stamp['ok']=result.returncode==0\n            stamp['returncode']=result.returncode", """            stamp['returncode']=result.returncode
            # A usable partial collection must not be reported as fully healthy.
            report_path=Path(config.STATE_DIR)/'research_refresh_report.json'
            try:
                report=json.loads(report_path.read_text())
                if datetime.fromisoformat(report['started_at']) < now:
                    raise ValueError('Stale child report')
                stamp['source_status']=report.get('status','unknown')
                stamp['source_errors']=len(report.get('errors',[]))
                stamp['source_skips']=len(report.get('skipped',[]))
                stamp['ok']=result.returncode==0 and stamp['source_status']=='healthy'
            except (ValueError,KeyError,TypeError,OSError):
                stamp['source_status']='missing_or_stale_report'
                stamp['ok']=False""")
s=s.replace("        return {'status':'refreshed' if stamp['ok'] else 'degraded',\n                'health':health(root,wanted=requested)}", "        return {'status':'refreshed' if stamp['ok'] else 'degraded',\n                'source_status':stamp.get('source_status','failed'),\n                'source_errors':stamp.get('source_errors',0),\n                'source_skips':stamp.get('source_skips',0),\n                'health':health(root,wanted=requested)}")
p.write_text(s)
p=root/'scripts/refresh_research.py';s=p.read_text();s=s.replace("        if previous and previous['content_sha256']==r['content_sha256']:\n            continue", """        quality_fields=('quality_warnings','source_tier','lineage_group','max_age_days')
        if (previous and previous['content_sha256']==r['content_sha256']
                and all(previous.get(k)==r.get(k) for k in quality_fields)):
            continue  # unchanged bytes AND quality; warnings themselves are evidence""");p.write_text(s)
p=root/'scripts/free_source_collection.py';s=p.read_text();s=s.replace("        value=float(row['value'])\n        if date>observed[:10] or not math.isfinite(value):continue", """        if date>observed[:10]:continue
        raw=str(row.get('value','')).strip()
        if raw.upper() in ('','-','--','N/A','NA','(NA)'):continue
        try:value=float(raw)
        except (ValueError,TypeError):raise ValueError('BLS non-numeric observation') from None
        if not math.isfinite(value):continue""")
s=s.replace("'detail':'Source failed validation or retrieval; prior good snapshots retained'})", """'detail':(str(exc)[:160] if isinstance(exc,ValueError)
                                         and str(exc).startswith(('BLS ','No eligible BLS'))
                                         else 'Source failed validation or retrieval; prior good snapshots retained')})""")
p.write_text(s)
p=root/'tests/test_research_automation.py';s=p.read_text();s=s.replace("    def test_runtime_calls_only_collector_with_timeout(self):\n        with", """    def test_runtime_calls_only_collector_with_timeout(self):
        def child(*args,**kwargs):
            (self.root/'research_refresh_report.json').write_text(json.dumps({
                'started_at':datetime.now(timezone.utc).isoformat(),'status':'healthy','errors':[],'skipped':[]}))
            return SimpleNamespace(returncode=0)
        with""")
s=s.replace("patch.object(auto.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:", "patch.object(auto.subprocess,'run',side_effect=child) as run:")
s=s.replace("    def test_future_source_health_excludes(self):", """    def test_partial_zero_exit_is_still_degraded(self):
        def child(*args,**kwargs):
            (self.root/'research_refresh_report.json').write_text(json.dumps({
                'started_at':datetime.now(timezone.utc).isoformat(),'status':'degraded',
                'errors':[{'source':'BLS'}],'skipped':[]}))
            return SimpleNamespace(returncode=0)
        with patch.object(config,'RESEARCH_AUTO_REFRESH',True),patch.object(config,'STATE_DIR',self.root),\
             patch.object(auto,'health',return_value={}),patch.object(auto.subprocess,'run',side_effect=child):
            result=auto.prepare_cycle(['AAPL'])
        self.assertEqual(result['status'],'degraded');self.assertEqual(result['source_errors'],1)
        self.assertFalse(json.loads((self.root/'research_cycle_refresh.json').read_text())['ok'])
    def test_quality_warning_change_is_preserved(self):
        feeds.persist([BASE],self.root)
        changed={**BASE,'observed_at':'2026-09-03T00:00:00+00:00','quality_warnings':['Mirror mismatch']}
        self.assertEqual(feeds.persist([changed],self.root),1)
        self.assertEqual(feeds.persist([changed],self.root),0)
        self.assertEqual(len(research_rag.load_records(self.root)),2)
    def test_future_source_health_excludes(self):""")
s=s.replace("    def test_mirror_disagreement_not_independent(self):", """    def test_bls_future_missing_value_does_not_drop_valid_series(self):
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
    def test_mirror_disagreement_not_independent(self):""")
p.write_text(s)
