"""Field-specific scalar thresholds; candidate suggestions never become effective values."""

from collections import defaultdict
from math import isfinite
from hashlib import sha256
import json


def candidate_fingerprint(candidates, *, model_revision, preprocess_version):
    data = {'candidates': [v.model_dump() for v in candidates], 'model_revision': model_revision,
            'preprocess_version': preprocess_version}
    return sha256(json.dumps(data, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _validate_rows(rows, split):
    originals = set()
    for row in rows:
        if row['split'] != split or not isinstance(row.get('group_id'), str) or not row['group_id']:
            raise ValueError('explicit source group and correct split required')
        if type(row['correct']) is not bool:
            raise ValueError('explicit adjudication required')
        if not all(isfinite(float(row[k])) for k in ('top_cosine', 'margin')) or not -1 <= row['top_cosine'] <= 1 or not 0 <= row['margin'] <= 2:
            raise ValueError('invalid calibration observation')
        originals.add(row['group_id'])
    return originals


def rank(scores, candidates, *, top_k, calibration=None, fingerprint=None):
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError('invalid top_k')
    if len(scores) != len(candidates) or any(not isfinite(float(score)) or not -1.00001 <= score <= 1.00001 for score in scores):
        raise ValueError('invalid cosine scores')
    groups = defaultdict(list)
    for score, candidate in zip(scores, candidates):
        groups[candidate.field].append({'concept_id': candidate.concept_id, 'text': candidate.text, 'cosine': float(score)})
    output = []
    for field, values in sorted(groups.items()):
        values.sort(key=lambda item: (-item['cosine'], item['concept_id']))
        rule = (calibration or {}).get(field)
        margin = values[0]['cosine'] - values[1]['cosine'] if len(values) > 1 else None
        usable = bool(fingerprint and rule and rule.get('state') == 'candidate_rule' and rule.get('fingerprint') == fingerprint)
        accepted = usable and margin is not None and values[0]['cosine'] >= rule['min_cosine'] and margin >= rule['min_margin']
        output.append({'field': field, 'state': 'candidate' if accepted else 'no_match' if usable else 'uncalibrated',
                       'top_k': values[:top_k], 'margin': margin, 'effective_value_written': False, 'score_kind': 'cosine_not_probability'})
    return output


def calibrate(rows, *, fingerprint):
    """Rows are calibration-only grouped originals; independent evaluation is never used to tune."""
    by_field = defaultdict(list)
    groups = _validate_rows(rows, 'calibration')
    for row in rows:
        by_field[row['field']].append(row)
    result = {}
    for field, samples in sorted(by_field.items()):
        positives = [r for r in samples if r['correct']]
        negatives = [r for r in samples if not r['correct']]
        if len({r['group_id'] for r in positives}) < 2 or len({r['group_id'] for r in negatives}) < 2:
            result[field] = {'state': 'insufficient_samples', 'positives': len(positives), 'negatives': len(negatives)}
            continue
        threshold = min(r['top_cosine'] for r in positives)
        margin = max(0.0, min(r['margin'] for r in positives))
        false_accepts = sum(r['top_cosine'] >= threshold and r['margin'] >= margin for r in negatives)
        result[field] = {'state': 'candidate_rule' if false_accepts == 0 else 'insufficient_separation',
                         'fingerprint': fingerprint, 'min_cosine': threshold, 'min_margin': margin,
                         'positives': len(positives), 'negatives': len(negatives), 'false_accepts': false_accepts,
                         'published': False, 'calibration_groups': sorted(groups)}
    return result


def evaluate(rows, rules, *, fingerprint):
    """An independent split reports counts; it cannot alter or publish a threshold."""
    groups = _validate_rows(rows, 'evaluation')
    if groups & {group for rule in rules.values() for group in rule.get('calibration_groups', [])}:
        raise ValueError('source group leakage between calibration and evaluation')
    result = {}
    for row in rows:
        rule = rules.get(row['field'], {})
        if rule.get('state') != 'candidate_rule' or rule.get('fingerprint') != fingerprint:
            raise ValueError('no matching calibrated rule')
        counts = result.setdefault(row['field'], {'samples': 0, 'accepted': 0, 'correct_accepted': 0, 'false_accepted': 0, 'rejected': 0})
        counts['samples'] += 1
        accepted = row['top_cosine'] >= rule['min_cosine'] and row['margin'] >= rule['min_margin']
        counts['accepted' if accepted else 'rejected'] += 1
        if accepted:
            counts['correct_accepted' if row['correct'] else 'false_accepted'] += 1
    return {'fields': result, 'published': False, 'business_acceptance': False}


def main(argv=None):
    import argparse
    from pathlib import Path
    import sys
    parser = argparse.ArgumentParser(description='Diagnostic single-choice field calibration; never publishes a vocabulary or rule')
    parser.add_argument('--observations', type=Path, required=True)
    parser.add_argument('--fingerprint', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rules', type=Path, help='Evaluate an independent split against frozen rules instead of tuning')
    args = parser.parse_args(argv)
    try:
        if not args.fingerprint or args.observations.stat().st_size > 4 * 1024**2:
            raise ValueError('invalid input')
        rows = json.loads(args.observations.read_text(encoding='utf-8'))
        if args.rules:
            if args.rules.stat().st_size > 4 * 1024**2:
                raise ValueError('invalid input')
            result = evaluate(rows, json.loads(args.rules.read_text(encoding='utf-8')), fingerprint=args.fingerprint)
        else:
            result = calibrate(rows, fingerprint=args.fingerprint)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=True, allow_nan=False, indent=2)
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print('{"error":"calibration_input_or_output_failed"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
