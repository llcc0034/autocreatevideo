"""The restricted REST adapter's pure top-level JSON field projection."""


def async_submit_body(payload, field_map):
    """Inputs are validated by the caller; absent/null are omitted, not falsy values.

    No headers, endpoint, credentials, defaults, or I/O belong in this projection.
    """
    return {target: payload[semantic] for semantic, target in field_map.items()
            if semantic in payload and payload[semantic] is not None}
