"""The duty-cycle budget is a legal limit, so it gets its own tests."""

from app.radio.airtime import AirtimeBudget


def test_eu868_limit_is_36_seconds_per_hour():
    assert AirtimeBudget(9600, 1.0).limit_seconds == 36.0


def test_airtime_scales_with_size_and_air_rate():
    slow, fast = AirtimeBudget(2400), AirtimeBudget(19200)
    assert slow.estimate(200) > fast.estimate(200)
    assert fast.estimate(400) > fast.estimate(200)


def test_budget_blocks_once_exhausted():
    budget = AirtimeBudget(9600, 1.0)
    for _ in range(200):
        budget.record(204)
    assert not budget.can_send(204)
    assert budget.remaining_seconds() == 0.0
    assert budget.fraction_used() == 1.0


def test_wait_time_is_reported_when_the_budget_is_full():
    budget = AirtimeBudget(9600, 1.0)
    for _ in range(200):
        budget.record(204)
    assert 0 < budget.wait_seconds(204) <= budget.window


def test_unlimited_mode_never_blocks():
    budget = AirtimeBudget(9600, 100.0)
    for _ in range(1000):
        budget.record(204)
    assert budget.can_send(204)
    assert budget.wait_seconds(204) == 0.0


def test_a_ten_second_voice_message_fits_many_times_per_hour():
    """The design claim: 700C voice is usable inside a 1% duty cycle."""
    budget = AirtimeBudget(9600, 1.0)
    ten_seconds_of_speech = 1000  # bytes, codec2 700C
    per_message = budget.estimate_message(ten_seconds_of_speech)
    assert budget.limit_seconds / per_message >= 15
