"""Cross-BTS common number analysis: A-party present in ≥2 tower windows."""

from __future__ import annotations

from collections import defaultdict

from cdr_report_app.bts.models import BtsWindowKey, CrossBtsCommon


def cross_bts_common(
    per_window_a: dict[str, set[str]],
    window_keys: list[BtsWindowKey],
    top: int = 20,
    per_window_counts: dict[str, dict[str, int]] | None = None,
) -> list[CrossBtsCommon]:
    """Find numbers that appear as A-party in ≥2 distinct windows/towers.

    *per_window_a*: mapping of window_key_id → set of A-party MSISDNs.
    *per_window_counts*: optional window_key_id → {msisdn → call_count}.
    """
    key_map = {_window_key_id(k): k for k in window_keys}

    # msisdn → {tower_label → total_calls}
    number_tower_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for key_id, numbers in per_window_a.items():
        key = key_map.get(key_id)
        tower_label = (key.bts_id or key.spec_id) if key else key_id
        counts = (per_window_counts or {}).get(key_id, {})
        for num in numbers:
            number_tower_counts[num][tower_label] += counts.get(num, 1)

    results = [
        CrossBtsCommon(
            msisdn=num,
            tower_count=len(tower_counts),
            towers=sorted(tower_counts.keys()),
            tower_call_counts=dict(tower_counts),
        )
        for num, tower_counts in number_tower_counts.items()
        if len(tower_counts) >= 2
    ]
    results.sort(key=lambda r: (r.tower_count, sum(r.tower_call_counts.values())), reverse=True)
    return results[:top] if top else results


def _window_key_id(key: BtsWindowKey) -> str:
    return f"{key.spec_id}|{key.window_start.isoformat()}|{key.window_end.isoformat()}"
