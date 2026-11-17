"""Regression tests for EOS classification in a running crossfade."""
import pytest
from mopidy.audio._gst.crossfade import should_ignore_spurious_eos

@pytest.mark.parametrize(
    ("position", "duration", "active", "pending", "expected"),
    [
        (5000, 168000, True, False, True),
        (160280, 168280, True, False, True),
        (167800, 168280, True, False, False),
        (168280, 168280, True, False, False),
        (0, None, True, False, False),
        (5000, 168000, False, False, False),
        (168280, 168280, True, True, True),
    ],
)
def test_should_ignore_spurious_eos(position, duration, active, pending, expected):
    assert should_ignore_spurious_eos(position, duration, active, pending) is expected
