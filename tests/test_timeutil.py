from datetime import datetime, timedelta, timezone

from app.timeutil import compute_duration_ms, to_naive_utc, utcnow

PLUS_3 = timezone(timedelta(hours=3))


def test_utcnow_is_naive_and_current():
    now = utcnow()

    assert now.tzinfo is None
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(reference - now) < timedelta(seconds=5)


def test_to_naive_utc_converts_aware_values():
    aware = datetime(2026, 10, 27, 12, 0, tzinfo=PLUS_3)

    assert to_naive_utc(aware) == datetime(2026, 10, 27, 9, 0)
    assert to_naive_utc(aware).tzinfo is None


def test_to_naive_utc_passes_naive_through_and_handles_none():
    naive = datetime(2026, 10, 27, 9, 0)

    assert to_naive_utc(naive) == naive
    assert to_naive_utc(None) is None


def test_duration_between_two_timestamps():
    start = datetime(2026, 10, 27, 9, 0, 0)
    end = datetime(2026, 10, 27, 9, 0, 1, 500_000)

    assert compute_duration_ms(start, end) == 1500


def test_duration_is_none_not_zero_when_a_timestamp_is_missing():
    moment = datetime(2026, 10, 27, 9, 0, 0)

    assert compute_duration_ms(moment, None) is None
    assert compute_duration_ms(None, moment) is None
    assert compute_duration_ms(None, None) is None


def test_zero_duration_is_a_real_zero():
    moment = datetime(2026, 10, 27, 9, 0, 0)

    assert compute_duration_ms(moment, moment) == 0


def test_duration_is_none_when_finished_before_started():
    start = datetime(2026, 10, 27, 9, 0, 5)
    end = datetime(2026, 10, 27, 9, 0, 0)

    assert compute_duration_ms(start, end) is None


def test_duration_handles_mixed_aware_and_naive_values():
    start = datetime(2026, 10, 27, 12, 0, 0, tzinfo=PLUS_3)  # 09:00 UTC
    end = datetime(2026, 10, 27, 9, 0, 2)  # naive, taken as UTC

    assert compute_duration_ms(start, end) == 2000
