"""Deterministic publication checks. No model, remote lookup, or vocabulary inference."""
from collections import defaultdict
import re
from media_tag.terminology.contracts import Taxonomy, Lexicon, normalize


def entries(payload):
    groups = ('options',) if isinstance(payload, Taxonomy) else ('concepts', 'aliases', 'relations')
    return [(group, entry) for group in groups for entry in getattr(payload, group)]


def issues(payload, contract, taxonomy=None, previous=()):
    problems = []
    def error(code, entry_id=None):
        item = {'code': code}
        if entry_id is not None: item['entry_id'] = entry_id
        if item not in problems: problems.append(item)

    all_entries = entries(payload)
    if not all_entries: error('empty_vocabulary')
    if payload.template_fingerprint != contract.fingerprint: error('template_mismatch')
    seen = set()
    for _, entry in all_entries:
        if entry.entry_id in seen: error('duplicate_entry_id', entry.entry_id)
        seen.add(entry.entry_id)
        if not entry.sources: error('missing_source', entry.entry_id)
    current_groups = {entry.entry_id:group for group,entry in all_entries}
    for old in previous:
        for group,entry in entries(old):
            if entry.entry_id in current_groups and current_groups[entry.entry_id] != group:
                error('entity_id_reused', entry.entry_id)
    if isinstance(payload, Taxonomy):
        definitions = {d.name:d for d in contract.field_definitions[0][1]}
        labels = set()
        options = {o.entry_id:o for o in payload.options}
        for o in payload.options:
            definition = definitions.get(o.field)
            closed=None
            if definition and o.field=='用途大类' and definition.field_type in ('单选','多选'):
                text=re.sub(r'（[^（）]*用）$','',definition.options_format)
                if '|' in text and not any(m in text for m in ('…','...','锁定列表')):closed=text.split('|')
            process_text=bool(definition and o.field=='过程/状态' and definition.field_type=='文本' and definition.selection_policy is None)
            if not definition or definition.selection_policy!='external_vocabulary' and not process_text and (closed is None or o.value not in closed):
                error('locked_template_field', o.entry_id)
            if o.value != o.value.strip() or any(m in o.value for m in ('…','...','|',';')): error('placeholder_option', o.entry_id)
            if o.status == 'active':
                key = (o.field, normalize(o.value))
                if key in labels: error('duplicate_option_value', o.entry_id)
                labels.add(key)
            _replacement(o, options, error)
        for old in previous:
            if not isinstance(old, Taxonomy): continue
            for o in old.options:
                new = options.get(o.entry_id)
                if new is None: error('retirement_required', o.entry_id)
                elif (o.field,o.value) != (new.field,new.value): error('option_id_reused', o.entry_id)
                elif o.status == 'retired' and new.status != 'retired': error('retired_id_reused', o.entry_id)
        return problems

    if taxonomy is None: error('missing_published_taxonomy'); return problems
    options = {o.entry_id:o for o in taxonomy.options if o.status == 'active'}
    concepts = {c.entry_id:c for c in payload.concepts}
    active = {k:v for k,v in concepts.items() if v.status == 'active'}
    for c in payload.concepts:
        if c.status == 'active' and not c.option_ids: error('missing_taxonomy_reference', c.entry_id)
        if len(set(c.option_ids)) != len(c.option_ids): error('duplicate_reference', c.entry_id)
        for oid in c.option_ids:
            if oid not in options and c.status == 'active': error('unpublished_taxonomy_reference', c.entry_id)
        expected = {'department':'科室','organ':'器官/系统','symptom':'疾病/症状','disease':'疾病/症状','process':'过程/状态','state':'过程/状态'}.get(c.concept_type)
        if expected and any(options[o].field != expected for o in c.option_ids if o in options): error('concept_type_mismatch', c.entry_id)
        _replacement(c, concepts, error)
    aliases = defaultdict(list)
    for a in payload.aliases:
        if a.target_id not in active: error('missing_active_target', a.entry_id)
        if a.default_expand and a.relation != 'equivalent': error('non_equivalent_expansion', a.entry_id)
        if a.relation == 'contextual' and a.context == type(a.context)(): error('missing_context', a.entry_id)
        for oid in a.context.departments:
            if oid not in options or options[oid].field != '科室': error('invalid_department_context', a.entry_id)
        for oid in a.context.organs:
            if oid not in options or options[oid].field != '器官/系统': error('invalid_organ_context', a.entry_id)
        if set(map(normalize,a.context.required_terms)) & set(map(normalize,a.context.excluded_terms)): error('contradictory_context', a.entry_id)
        context=a.context.model_dump(mode='json')
        for field in ('departments','organs'):context[field]=sorted(set(context[field]))
        for field in ('required_terms','excluded_terms'):context[field]=sorted(set(map(normalize,context[field])))
        from media_tag.catalog.service import encode
        key = (normalize(a.expression), encode(context))
        aliases[key].append(a)
    for group in aliases.values():
        signatures = [(a.target_id,a.relation) for a in group]
        if len(set(signatures)) != len(signatures): error('duplicate_mapping', group[-1].entry_id)
        if len({a.target_id for a in group}) > 1:
            groups = {a.ambiguity_group for a in group}
            if None in groups or len(groups) != 1: error('unacknowledged_ambiguity', group[-1].entry_id)
            if any(a.default_expand for a in group): error('ambiguous_default_expansion', group[-1].entry_id)
    hierarchy = defaultdict(set)
    relation_keys = set()
    for r in payload.relations:
        if r.source_id not in active or r.target_id not in active: error('missing_active_target', r.entry_id)
        if r.source_id == r.target_id: error('self_relation', r.entry_id)
        if r.default_expand and r.relation != 'equivalent': error('non_equivalent_expansion', r.entry_id)
        # contextual relations need per-expression conditions, represented by Alias.
        if r.relation == 'contextual': error('contextual_relation_requires_alias', r.entry_id)
        key = (r.source_id,r.target_id,r.relation)
        if key in relation_keys: error('duplicate_relation', r.entry_id)
        relation_keys.add(key)
        # source broader target means source is the parent; narrower reverses the arc.
        if r.relation == 'broader': hierarchy[r.source_id].add(r.target_id)
        if r.relation == 'narrower': hierarchy[r.target_id].add(r.source_id)
    # Iterative Kahn traversal avoids recursion overflow for long medical hierarchies.
    nodes = set(hierarchy) | {v for targets in hierarchy.values() for v in targets}
    degrees = {n:0 for n in nodes}
    for targets in hierarchy.values():
        for target in targets: degrees[target] += 1
    queue = [n for n,d in degrees.items() if d == 0]; count = 0
    while queue:
        n = queue.pop(); count += 1
        for target in hierarchy[n]:
            degrees[target] -= 1
            if degrees[target] == 0: queue.append(target)
    if count != len(nodes): error('hierarchy_cycle')
    # Equivalent concepts cannot contradict type or taxonomy meaning.
    for r in payload.relations:
        if r.relation == 'equivalent' and r.source_id in active and r.target_id in active:
            a,b = active[r.source_id],active[r.target_id]
            if a.concept_type != b.concept_type or set(a.option_ids) != set(b.option_ids): error('conflicting_equivalence', r.entry_id)
    for old in previous:
        if not isinstance(old, Lexicon): continue
        for c in old.concepts:
            new = concepts.get(c.entry_id)
            if new is None: error('retirement_required', c.entry_id)
            elif c.status == 'retired' and new.status != 'retired': error('retired_id_reused', c.entry_id)
            elif c.concept_type != new.concept_type: error('concept_id_reused', c.entry_id)
            elif c.name != new.name and c.name not in new.old_names: error('rename_requires_old_name', c.entry_id)
            elif not set(c.old_names)<=set(new.old_names): error('old_names_must_be_preserved', c.entry_id)
    return problems


def _replacement(entry, objects, error):
    if entry.status == 'active' and entry.replacements: error('active_replacement', entry.entry_id)
    if len(set(entry.replacements)) != len(entry.replacements): error('duplicate_reference', entry.entry_id)
    for target in entry.replacements:
        if target == entry.entry_id or target not in objects or objects[target].status != 'active': error('invalid_migration_target', entry.entry_id)
        elif hasattr(entry,'field') and objects[target].field != entry.field: error('migration_field_mismatch', entry.entry_id)


def option_fields(payload,contract=None):
    """D06 projection: only active options, preserving the original field labels."""
    fields = defaultdict(list)
    external={d.name for d in contract.field_definitions[0][1] if d.selection_policy=='external_vocabulary'} if contract else None
    for option in payload.options:
        if option.status == 'active' and (external is None or option.field in external): fields[option.field].append(option.value)
    return dict(fields)
