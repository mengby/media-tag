"""No zero-denominator passes, no omission of failures from recall or latency."""
import math
from collections import Counter


def fraction(numerator,denominator):
    return {'numerator':numerator,'denominator':denominator,'value':numerator/denominator if denominator else None}


def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None


def score(case,response,*,elapsed_seconds,error=None,semantic=True):
    resolution=(response or {}).get('resolution',{});state=resolution.get('state')
    failure=error or (('text_model_timeout' if resolution.get('model_state')=='timed_out' else 'terminology_degraded' if state=='degraded' else None) if semantic else None)
    selected=set((response or {}).get('selected_concept_ids',[])) if semantic else set()
    clear=semantic and state=='resolved' and not failure
    context=(response or {}).get('intent',{})
    direction_wrong=bool(selected and case.expected_direction is not None and context.get('direction')!=case.expected_direction)
    polarity_wrong=bool(selected and case.expected_polarity is not None and context.get('polarity')!=case.expected_polarity)
    wrong_expansion=bool(selected-set(case.allowed_automatic_concepts)) or direction_wrong or polarity_wrong
    # One fixed version represents each root, exactly as the gallery chooses its default.
    ranked=[g['versions'][-1]['asset_id'] for g in (response or {}).get('items',[])[:10] if g.get('versions')]
    forbidden_assets=sorted(set(ranked)&set(case.forbidden_asset_ids))
    candidates=[c['concept_id'] for c in resolution.get('candidates',[])[:5]]
    relevant=set(case.relevant_asset_ids);known=set(case.acceptable_concepts)
    correct_clear=bool(clear and case.expectation=='clear' and selected and not wrong_expansion)
    expected=correct_clear if case.expectation=='clear' else not failure and state==('ambiguous' if case.expectation=='clarify' else 'no_match') and not selected
    return {'case_id':case.case_id,'category':case.category,'source_group':case.source_group,'state':state,
        'failure':failure,'elapsed_seconds':elapsed_seconds,'clear':bool(clear),
        'correct_clear':correct_clear,'expectation_met':bool(expected) if semantic else None,'actual_intent':context,
        'model_state':resolution.get('model_state'),'resolution_reason':resolution.get('reason'),
        'common':case.category=='common' and case.expectation=='clear',
        'rejected':not failure and state=='no_match','ambiguous':not failure and state=='ambiguous',
        'concept_recall5':len(set(candidates)&known)/len(known) if known and not failure else 0 if known else None,
        'asset_hit10':bool(set(ranked)&relevant) and not failure if relevant else None,
        'asset_recall10':len(set(ranked)&relevant)/len(relevant) if relevant and case.complete_relevant_set and not failure else 0 if relevant and case.complete_relevant_set else None,
        'automatic_expansion_error':wrong_expansion,'forbidden_concept_expansion':sorted(selected&set(case.forbidden_concepts)),
        'direction_error':direction_wrong,'polarity_error':polarity_wrong,'forbidden_asset_hits':forbidden_assets,
        'candidate_ids':candidates,'selected_concept_ids':sorted(selected),'ranked_asset_ids':ranked}


def aggregate(rows,*,semantic=True):
    recalls=[r['concept_recall5'] for r in rows if r['concept_recall5'] is not None]
    relevant=[r for r in rows if r['asset_hit10'] is not None]
    complete=[r['asset_recall10'] for r in rows if r['asset_recall10'] is not None]
    elapsed=[r['elapsed_seconds'] for r in rows]
    errors=Counter(r['failure'] for r in rows if r['failure'])
    return {'queries':len(rows),'precision':fraction(sum(r['correct_clear'] for r in rows),sum(r['clear'] for r in rows)) if semantic else None,
        'common_coverage':fraction(sum(r['clear'] and r['common'] for r in rows),sum(r['common'] for r in rows)) if semantic else None,
        'concept_recall5':{'macro_mean':sum(recalls)/len(recalls) if recalls else None,'queries':len(recalls)} if semantic else None,
        'asset_hit10':fraction(sum(bool(r['asset_hit10']) for r in relevant),len(relevant)),
        'queries_without_visible_relevant_assets':len(rows)-len(relevant),
        'asset_recall10':{'macro_mean':sum(complete)/len(complete),'queries':len(complete)} if complete else None,
        'expectation_agreement':fraction(sum(bool(r['expectation_met']) for r in rows),len(rows)) if semantic else None,
        'rejection_rate':fraction(sum(r['rejected'] for r in rows),len(rows)) if semantic else None,
        'ambiguity_rate':fraction(sum(r['ambiguous'] for r in rows),len(rows)) if semantic else None,
        'failures':dict(sorted(errors.items())),'failure_rate':fraction(sum(errors.values()),len(rows)),
        'automatic_expansion_errors':sum(r['automatic_expansion_error'] for r in rows),
        'forbidden_concept_expansions':sum(bool(r['forbidden_concept_expansion']) for r in rows),
        'direction_errors':sum(r['direction_error'] for r in rows),'polarity_errors':sum(r['polarity_error'] for r in rows),
        'forbidden_asset_hits':sum(bool(r['forbidden_asset_hits']) for r in rows),
        'latency_seconds':{'samples_including_errors':len(elapsed),'p50':percentile(elapsed,.5),'p95':percentile(elapsed,.95),'max':max(elapsed) if elapsed else None}}


def report(rows,criteria,*,semantic=True):
    result=aggregate(rows,semantic=semantic)
    result['by_category']={category:aggregate([r for r in rows if r['category']==category],semantic=semantic) for category in sorted({r['category'] for r in rows})}
    def meets(value,target):return value is not None and value>=target
    result['proposed_metric_gates_passed']=bool(semantic and meets(result['precision']['value'],criteria.precision)
        and meets(result['common_coverage']['value'],criteria.common_coverage)
        and meets(result['concept_recall5']['macro_mean'],criteria.concept_recall5)
        and meets(result['asset_hit10']['value'],criteria.asset_hit10)
        and not any(result[key] for key in ('failures','automatic_expansion_errors','forbidden_asset_hits')))
    return result
