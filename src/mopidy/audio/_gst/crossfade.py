import math


def calculate_gains(
    progress: float,
    curve: str = "linear",
) -> tuple[float, float]:

    if not 0.0 <= progress <= 1.0:
        raise ValueError("Progress must be between 0 and 1")

    if curve == "linear":
        gain_a = 1.0 - progress
        gain_b = progress
        return gain_a, gain_b

    elif curve == "equal_power":
        gain_a = math.cos(progress * math.pi / 2)
        gain_b = math.sin(progress * math.pi / 2)
        return gain_a, gain_b

    else:
        raise ValueError(f"Unsupported curve: {curve}")

def should_start_crossfade(
    position_ms: int,
    duration_ms: int | None,
    crossfade_seconds: int,
    has_next_track: bool,
) -> bool:

    if crossfade_seconds <= 0:
        return False

    if not has_next_track or duration_ms is None:
        return False

    crossfade_ms = crossfade_seconds * 1000

    if duration_ms <= crossfade_ms:
        return False

    remaining_ms = duration_ms - position_ms

    return 0 < remaining_ms <= crossfade_ms


def should_ignore_spurious_eos(
    position_ms: int,
    duration_ms: int | None,
    has_dynamic_track: bool,
    has_pending_crossfade: bool,
    tolerance_ms: int = 1000,
) -> bool:
    """Ignore a retired playbin EOS while a newer source is still playing.

    An EOS after the actual current song finishes should not be hidden.
    """
    if has_pending_crossfade:
        return True
    if not has_dynamic_track or duration_ms is None:
        return False
    return position_ms + tolerance_ms < duration_ms
