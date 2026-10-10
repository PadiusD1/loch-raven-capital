"""Reproducible retrieval evaluation; never calls a model or places an order.

These curated answer-location probes test retrieval, not investment returns.
The QA export contains relevant evidence, distractors and explicit abstentions;
it is a reviewable training candidate, not an automatically promoted model.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from firm.research_rag import load_records, retrieve, corpus_status, desk_evidence

CASES = [
 ('fundamental','AAPL','Apple quarterly revenue and tariff refund impact on margin fiscal Q3 2026','apple-fy26q3-results'),
 ('fundamental','NVDA','NVIDIA quarterly operating cash flow GAAP net income fiscal Q2 2027','nvidia-fy27q2-results'),
 ('fundamental','MSFT','Microsoft GAAP and adjusted diluted earnings per share June 2026','microsoft-fy26q4-results'),
 ('sentiment','NVDA','NVIDIA next quarter revenue guidance excludes China Data Center','nvidia-fy27q3-guidance'),
 ('sentiment','NVDA','NVIDIA future science quantum healthcare commitment October','nvidia-science-commitment-20261008'),
 ('macro','AAPL','September 16 2026 FOMC policy target range rate increase','fed-20260916-policy'),
 ('technical','AAPL','time-series momentum 58 futures contracts 12-month returns reversal','time-series-momentum-evidence'),
 ('quant','AAPL','French monthly momentum factor size prior months 2-12 construction','french-momentum-construction'),
 ('macro','MSFT','FRED ALFRED historical revisions real-time observation publication dates','fred-revision-discipline'),
 ('quant','MSFT','RAFT open book relevant evidence distractor training evaluation','raft-evidence-training'),
]


def evaluate(root: Path, out: Path) -> dict:
    records = load_records(root)
    by_id = {r['id']: r for r in records}
    if not records:
        raise ValueError('Cannot evaluate an empty corpus')
    asof = (datetime.fromisoformat(max(r['observed_at'] for r in records))+timedelta(seconds=1)).isoformat()
    rows, training = [], []
    for desk,ticker,query,target in CASES:
        hits = retrieve(query,desk=desk,ticker=ticker,asof=asof,k=5,root=root)
        ids=[h['record']['id'] for h in hits]
        rank=ids.index(target)+1 if target in ids else None
        rows.append({'desk':desk,'ticker':ticker,'query':query,'expected_id':target,'rank':rank,
                     'hit_at_3':rank is not None and rank<=3,'returned_ids':ids})
        source = by_id[target]
        relevant = [source]+[h['record'] for h in hits if h['record']['id']!=target][:2]
        # Evidence order varied deterministically, so the answer is not always source 1.
        relevant.sort(key=lambda r: hashlib.sha256((query+r['id']).encode()).hexdigest())
        context=[{'id':r['id'],'text':r['body'],'url':r['source_url']} for r in relevant]
        training.append({'question':query,'context':context,
                         'answer':source['body'],'citations':[target],
                         'metadata':{'desk':desk,'available_at':asof,'split_group':source['source_url'],
                                     'status':'candidate_requires_review','training_task':'evidence_grounding_not_return_prediction'}})
        training.append({'question':query,'context':[c for c in context if c['id']!=target],
                         'answer':'Insufficient evidence in the supplied documents to answer this question.',
                         'citations':[], 'metadata':{'desk':desk,'available_at':asof,
                         'split_group':source['source_url'],'status':'candidate_requires_review',
                         'training_task':'missing_source_abstention'}})
    earliest = min(datetime.fromisoformat(r['observed_at']) for r in records)
    before = (earliest-timedelta(seconds=1)).isoformat()
    future_excluded=all(not retrieve(query,desk=desk,ticker=ticker,asof=before,k=3,root=root)
                        for desk,ticker,query,_ in CASES)
    coverage={desk:len(desk_evidence(desk, {'ticker':'AAPL','asof':asof},root=root))
              for desk in ('fundamental','technical','sentiment','macro','quant')}
    result={'evaluated_at':datetime.now(timezone.utc).isoformat(),'corpus':corpus_status(root),
            'curated_cases':len(rows),'hit_at_3':sum(r['hit_at_3'] for r in rows)/len(rows),
            'mean_reciprocal_rank_at_5':sum(1/r['rank'] if r['rank'] else 0 for r in rows)/len(rows),
            'future_records_excluded':future_excluded,'desk_evidence_counts':coverage,
            'cases':rows,'training_candidates':len(training),'models_trained':0,
            'promote_model':False,'evaluation_split':'development_probes_not_held_out',
            'training_warning':'Grounding candidates overlap development probes. Do not train on them and call these tests held-out.',
            'limitation':'Curated retrieval probes, not model-answer accuracy or investment performance.'}
    out.mkdir(parents=True,exist_ok=True)
    (out/'retrieval_evaluation.json').write_text(json.dumps(result,indent=2)+'\n')
    (out/'grounding_candidates.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in training))
    if not future_excluded or not all(coverage.values()) or result['hit_at_3'] < 0.8:
        raise AssertionError('Research integrity/retrieval acceptance checks failed; inspect report')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('data/research'))
    p.add_argument('--out',type=Path,default=Path('data/research_evaluation'));a=p.parse_args()
    print(json.dumps(evaluate(a.root,a.out),indent=2))
