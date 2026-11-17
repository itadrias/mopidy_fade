import math

import pytest

from mopidy.audio._gst.crossfade import calculate_gains


def test_linear_crossfade():
    assert calculate_gains(0.0, "linear") == (1.0, 0.0)
    assert calculate_gains(0.5, "linear") == (0.5, 0.5)
    assert calculate_gains(1.0, "linear") == (0.0, 1.0)


def test_equal_power_crossfade():
    assert calculate_gains(0.0, "equal_power") == pytest.approx((1.0, 0.0))
    assert calculate_gains(0.5, "equal_power") == pytest.approx(
        (math.sqrt(0.5), math.sqrt(0.5))
    )
    assert calculate_gains(1.0, "equal_power") == pytest.approx((0.0, 1.0))


@pytest.mark.parametrize("progress", [-0.1, 1.1])
def test_invalid_progress(progress):
    with pytest.raises(ValueError):
        calculate_gains(progress)


def test_invalid_curve():
    with pytest.raises(ValueError):
        calculate_gains(0.5, "unknown")

from mopidy.audio._gst.crossfade import should_start_crossfade


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (0, False),
        (54000, False),
        (55000, True),
        (57500, True),
        (59999, True),
        (60000, False),
    ],
)
def test_crossfade_start_time(position, expected):
    assert should_start_crossfade(
        position_ms=position,
        duration_ms=60000,
        crossfade_seconds=5,
        has_next_track=True,
    ) is expected


def test_crossfade_disabled():
    assert not should_start_crossfade(58000, 60000, 0, True)


def test_crossfade_without_next_track():
    assert not should_start_crossfade(58000, 60000, 5, False)


def test_crossfade_unknown_duration():
    assert not should_start_crossfade(58000, None, 5, True)
