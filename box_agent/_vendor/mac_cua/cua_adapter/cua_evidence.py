"""Standalone CUA adapter evidence wire contract, shared by guest and host audit.

This module has no package/runtime dependencies so the exact same source can
be deployed beside cua_adapter.py in an isolated guest.
"""

FORMAT_VERSION = 7
SUPPORTED_VERSIONS = (2, 3, 4, 5, 6, 7)
DRIVER_PHASES = frozenset({'dispatch', 'result', 'error'})
CELL_PHASES = frozenset({'cell_start', 'cell_end'})
DIAGNOSTIC_FIELDS = {
    'keyboard_activation_reconciled': {'window_id': int, 'activation_report': dict},
    'main_window_fallback': {'window_id': int, 'source_window_id': int, 'reason': str},
    'window_relationships': {'pid': int, 'current_window_id': int, 'windows': list,
                             'ax_sources': list, 'attachments': dict, 'conflicts': list},
    'keyboard_focus_refreshed': {'window_id': int, 'chain': list,
                                  'focus_reported': (bool, type(None)),
                                  'focus_count': (int, type(None))},
    'keyboard_focus_rejected': {'window_id': int, 'reason': str},
    'attachment_verified': {'window_id': int, 'chain': list, 'front_chain': list,
                            'keyboard': bool, 'focus_evidence': str, 'edges': list},
    'attachment_rejected': {'reason': str},
}

BACKGROUND_FIELDS = {
    'foreground_fallback': {'tool': str, 'reason': str, 'pid': int,
                            'active_window_id': int, 'source_window_id': int},
    'pixel_fallback_foregrounded': {'window_id': int},
}
DIAGNOSTIC_FIELDS.update(BACKGROUND_FIELDS)

POPUP_FOCUS_FIELDS = {
    'popup_focus_candidate': {'source_window_id': int,
                              'delivery_path': (str, type(None)), 'effect': str},
    'popup_focus_candidate_rejected': {'source_window_id': int, 'added_window_ids': list,
                                       'focused_window_id': (int, type(None))},
    'popup_focus_relation_created': {'surface_window_id': int, 'focus_owner_window_id': int,
                                     'windows': list},
    'popup_focus_relation_rejected': {'surface_window_id': int, 'focus_owner_window_id': int,
                                      'reason': str},
    'popup_focus_relation_refreshed': {'surface_window_id': int, 'focus_owner_window_id': int},
    'popup_focus_fallback': {'surface_window_id': int, 'focus_owner_window_id': int,
                             'screen_x': (int, float), 'screen_y': (int, float),
                             'button': str, 'count': int},
    'popup_focus_fallback_rejected': {'surface_window_id': int, 'focus_owner_window_id': int,
                                      'reason': str},
    'popup_focus_fallback_delivered': {'surface_window_id': int, 'focus_owner_window_id': int},
}
DIAGNOSTIC_FIELDS.update(POPUP_FOCUS_FIELDS)


def validate_phase(row: dict, version: int) -> None:
    """Validate version-specific phases and native diagnostic payloads.

    Cell identity, ordering and driver pairing belong to the stream auditor.
    Diagnostics do not stand in for a dispatch, outcome or cell receipt.
    """
    phase = row.get('phase')
    if version not in SUPPORTED_VERSIONS or not isinstance(phase, str):
        raise ValueError('Unsupported GUI evidence format or phase')
    if phase in DRIVER_PHASES or phase in CELL_PHASES:
        return
    if (version < 4 or phase not in DIAGNOSTIC_FIELDS
            or (version < 5 and phase == 'main_window_fallback')
            or (version < 6 and phase in BACKGROUND_FIELDS)
            or (version < 7 and phase in POPUP_FOCUS_FIELDS)
            or row.get('backend') != 'native'):
        raise ValueError('Unknown GUI evidence diagnostic')
    for field, allowed in DIAGNOSTIC_FIELDS[phase].items():
        types = allowed if isinstance(allowed, tuple) else (allowed,)
        if field not in row or type(row[field]) not in types:
            raise ValueError('Invalid GUI diagnostic field: ' + field)
    if 'window_id' in row and type(row['window_id']) is not int:
        raise ValueError('Invalid GUI diagnostic window identity')
