"""Conservative first-frame routing until independent exemption calibration exists."""


def route_local_visual(value, content_kind):
    if content_kind not in ('image', 'video', 'animation') or value.state == 'not_applicable':
        raise ValueError('local visual routing requires an applicable visual input')
    if value.state == 'no_match':
        return 'vocabulary', 'local_candidate_outside_published_options'
    if value.state == 'failed':
        return 'local', 'local_visual_analysis_requires_repair'
    if content_kind in ('video', 'animation'):
        return 'evidence', 'first_frame_only_continuous_evidence_required'
    return 'precision', 'local_draft_not_calibrated_for_precision_exemption'
