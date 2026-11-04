import numpy as np
import pandas as pd

from donchian.channel import atr, donchian, true_range


def s(values):
    return pd.Series(values, dtype="float64")


def test_channel_excludes_the_current_bar():
    """The shift-by-1 boundary: bar i must not be part of its own channel."""
    high = s([10, 11, 12, 50, 13])
    low = s([8, 9, 10, 1, 11])
    ch = donchian(high, low, 3)

    assert ch["upper"].iloc[:3].isna().all()          # fewer than 3 prior bars
    assert ch["upper"].iloc[3] == 12                  # max(10, 11, 12): the 50 on bar 3 is NOT in it
    assert ch["lower"].iloc[3] == 8                   # min(8, 9, 10): the 1 on bar 3 is NOT in it
    assert ch["upper"].iloc[4] == 50                  # the next bar sees it
    assert ch["lower"].iloc[4] == 1
    # the un-shifted rolling max would have included the current bar
    assert high.rolling(3).max().iloc[3] == 50 != ch["upper"].iloc[3]


def test_channel_trails_a_rising_high_by_exactly_one_bar():
    high = s([1, 2, 3, 4, 5, 6, 7])
    ch = donchian(high, high - 1, 2)
    # upper[i] = max(high[i-2:i]) = high[i-1] for a rising series: it steps up one bar late
    assert ch["upper"].iloc[2:].tolist() == [2.0, 3.0, 4.0, 5.0, 6.0]
    assert ch["upper"].iloc[:2].isna().all()


def test_mid_is_average_of_bands():
    high, low = s([10, 12, 11, 13]), s([8, 9, 7, 10])
    ch = donchian(high, low, 2)
    assert ch["upper"].iloc[2:].tolist() == [12.0, 12.0] and ch["lower"].iloc[2:].tolist() == [8.0, 7.0]
    assert ch["mid"].iloc[2:].tolist() == [10.0, 9.5]


def test_fewer_bars_than_n_is_all_nan_and_exactly_n_bars_too():
    for count in (3, 5):  # n = 5: 3 bars is too few; exactly 5 bars still has no 5 PRIOR bars
        ch = donchian(s(range(1, count + 1)), s(range(0, count)), 5)
        assert ch.isna().all().all()
    ch = donchian(s(range(1, 7)), s(range(0, 6)), 5)      # 6 bars: the last has 5 prior bars
    assert ch["upper"].iloc[5] == 5 and ch.iloc[:5].isna().all().all()


def test_true_range_uses_prev_close_on_gaps():
    high, low, close = s([11, 15]), s([9, 14]), s([10, 14.5])
    tr = true_range(high, low, close)
    assert tr.iloc[0] == 2          # first bar: high - low
    assert tr.iloc[1] == 5          # gap up: |15 - 10| beats 15 - 14


def test_atr_of_constant_range_equals_the_range():
    n = 5
    high, low, close = s([12] * 20), s([10] * 20), s([11] * 20)
    a = atr(high, low, close, n)
    assert a.iloc[: n - 1].isna().all()
    assert np.allclose(a.iloc[n - 1 :], 2.0)
