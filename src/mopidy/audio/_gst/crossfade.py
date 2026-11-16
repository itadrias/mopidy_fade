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
