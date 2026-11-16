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
