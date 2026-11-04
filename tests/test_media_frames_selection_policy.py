"""Counterexamples for the frozen orientation policy, not a detector scorecard."""

from fractions import Fraction

from PIL import Image
import pytest

from pulsara_agent.media.frames import (
    overview_allocation,
    overview_probe_count,
    scene_dimensions,
)
from pulsara_agent.media.frames_selection import Sample, overview_priority


def samples(colors, times=None):
    return [
        Sample(
            Fraction(i) if times is None else times[i],
            Image.new("RGB", (256, 144), color),
            i + 1,
        )
        for i, color in enumerate(colors)
    ]


def test_static_observations_stop_at_distinct_temporal_endpoints():
    observed = samples(["white"] * 31)
    assert overview_priority(observed, 16) == (0, 30)
    assert overview_priority(observed[:1], 16) == (0,)


def test_recurrence_keeps_later_time_without_global_deduplication():
    observed = samples(["red"] * 10 + ["blue"] * 10 + ["red"] * 11)
    chosen = overview_priority(observed, 16)
    assert chosen[:2] == (0, 30)
    assert len(chosen) == 3
    assert 10 <= chosen[2] < 20


def test_fixed_first_group_comparison_does_not_chain_slow_drift():
    observed = samples([(40 + 3 * i,) * 3 for i in range(31)])
    chosen = overview_priority(observed, 16)
    assert chosen[:2] == (0, 30)
    assert 2 < len(chosen) < 16
    assert any(8 <= i <= 20 for i in chosen)


def test_largest_eligible_gap_precedes_nearest_selected_distance():
    # After 5.1 is selected, [0,5.1] is the larger gap but its only
    # remaining candidate is near the boundary. The frozen contract
    # refines that eligible gap, without claiming a minimax optimum.
    times = [
        Fraction(0),
        Fraction(1, 10),
        Fraction(51, 10),
        Fraction(151, 20),
        Fraction(10),
    ]
    observed = samples(["red", "blue", "green", "white", "black"], times)
    assert overview_priority(observed, 4) == (0, 4, 2, 1)


@pytest.mark.parametrize(
    "aspect, expected",
    [
        (Fraction(16, 9), (320, 180)),
        (Fraction(9, 16), (180, 320)),
        (Fraction(1), (320, 320)),
        (Fraction(4), (720, 180)),
        (Fraction(1, 4), (180, 720)),
    ],
)
def test_target_preserves_both_axes_for_extreme_aspects(aspect, expected):
    assert scene_dimensions(aspect) == expected
    assert scene_dimensions(aspect, floor=True) == tuple(v // 2 for v in expected)


def test_profile_probe_work_is_duration_aware_but_not_duration_limited():
    aspect = Fraction(16, 9)
    assert overview_allocation(aspect) == 16
    assert overview_probe_count(aspect, Fraction(1, 2)) == 3
    assert overview_probe_count(aspect, Fraction(334, 15)) == 24
    assert overview_probe_count(aspect, 3600) == 31
    assert overview_probe_count(aspect, 10**12) == 31
