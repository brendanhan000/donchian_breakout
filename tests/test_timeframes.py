from datetime import date

import pandas as pd
import pytest

from donchian.timeframes import (
    MARKET_TZ,
    drop_incomplete,
    raw_to_frame,
    resample_intraday,
    to_frame,
)
from tests.helpers import et, raw_30m, raw_daily

HALF_DAY = date(2026, 11, 27)          # Friday after Thanksgiving: closes 13:00 ET
NORMAL_DAY = date(2026, 11, 25)


def local(index):
    return [t.tz_convert(MARKET_TZ).strftime("%H:%M") for t in index]


# ------------------------------------------------------------- daily -> weekly


def weekdays(start: date, n_weeks: int) -> list[date]:
    days = pd.bdate_range(start, periods=5 * n_weeks)
    return [d.date() for d in days]


def test_daily_session_date_is_kept_from_the_schwab_stamp():
    summer = raw_to_frame(raw_daily([date(2026, 9, 18)], stamp_hour_utc=5), "1d")
    winter = raw_to_frame(raw_daily([date(2026, 1, 16)], stamp_hour_utc=6), "1d")
    assert summer.index[0] == pd.Timestamp("2026-09-18", tz="UTC")
    assert winter.index[0] == pd.Timestamp("2026-01-16", tz="UTC")


def test_daily_to_weekly_w_fri_ohlcv():
    raw = raw_daily(weekdays(date(2026, 9, 14), 2))          # Mon 09-14 .. Fri 09-25
    w = to_frame(raw, "1W", as_of=et(2026, 10, 5, 12))
    assert list(w.index) == [pd.Timestamp("2026-09-18", tz="UTC"), pd.Timestamp("2026-09-25", tz="UTC")]
    wk1, wk2 = w.iloc[0], w.iloc[1]
    # bars d=0..4: open 100+d, high 110+d, low 90+d, close 105+d, volume 1000*(d+1)
    assert (wk1["open"], wk1["high"], wk1["low"], wk1["close"], wk1["volume"]) == (100, 114, 90, 109, 15000)
    assert (wk2["open"], wk2["high"], wk2["low"], wk2["close"], wk2["volume"]) == (105, 119, 95, 114, 40000)
    assert w["complete"].all()


def test_weekly_labels_the_friday_even_when_friday_is_a_holiday():
    good_friday_week = [date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 2)]   # Fri 04-03 closed
    w = to_frame(raw_daily(good_friday_week), "1W", as_of=et(2026, 4, 10, 12))
    assert list(w.index) == [pd.Timestamp("2026-04-03", tz="UTC")]
    assert (w.iloc[0]["open"], w.iloc[0]["close"], w.iloc[0]["volume"]) == (100, 108, 10000)


def test_weekly_bar_is_incomplete_until_friday_close():
    raw = raw_daily(weekdays(date(2026, 9, 14), 2))
    before = to_frame(raw, "1W", as_of=et(2026, 9, 25, 15, 0))
    after = to_frame(raw, "1W", as_of=et(2026, 9, 25, 16, 1))
    assert list(before["complete"]) == [True, False]
    assert list(after["complete"]) == [True, True]
    assert len(drop_incomplete(before)) == 1


def test_daily_bar_is_incomplete_until_that_sessions_close():
    raw = raw_daily([date(2026, 9, 17), date(2026, 9, 18)])
    d = to_frame(raw, "1D", as_of=et(2026, 9, 18, 15, 59))
    assert list(d["complete"]) == [True, False]
    d = to_frame(raw, "1D", as_of=et(2026, 9, 18, 16, 1))
    assert list(d["complete"]) == [True, True]


# ------------------------------------------------- session-anchored 1H / 4H


def test_1h_buckets_are_anchored_to_the_open_across_a_normal_and_a_half_day():
    raw = pd.concat([raw_30m(NORMAL_DAY, 13), raw_30m(HALF_DAY, 7)], ignore_index=True)
    h = to_frame(raw, "1H", as_of=et(2026, 11, 30, 12), early_closes=frozenset({HALF_DAY}))
    normal = h[h.index.tz_convert(MARKET_TZ).date == NORMAL_DAY]
    half = h[h.index.tz_convert(MARKET_TZ).date == HALF_DAY]
    assert local(normal.index) == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert local(half.index) == ["09:30", "10:30", "11:30", "12:30"]
    # 30m bar k has volume 1000+k, so hourly buckets sum pairs; the trailing half-hour stands alone
    assert list(normal["volume"]) == [2001, 2005, 2009, 2013, 2017, 2021, 1012]
    assert list(half["volume"]) == [2001, 2005, 2009, 1006]
    assert h["complete"].all()


def test_4h_buckets_split_a_normal_session_and_keep_a_half_day_whole():
    raw = pd.concat([raw_30m(NORMAL_DAY, 13), raw_30m(HALF_DAY, 7)], ignore_index=True)
    f = to_frame(raw, "4H", as_of=et(2026, 11, 30, 12), early_closes=frozenset({HALF_DAY}))
    normal = f[f.index.tz_convert(MARKET_TZ).date == NORMAL_DAY]
    half = f[f.index.tz_convert(MARKET_TZ).date == HALF_DAY]
    assert local(normal.index) == ["09:30", "13:30"]
    assert local(half.index) == ["09:30"]
    morning = normal.iloc[0]        # bars k = 0..7 (09:30..13:00)
    assert (morning["open"], morning["high"], morning["low"], morning["close"]) == (100, 107.5, 99.5, 107.25)
    assert morning["volume"] == sum(1000 + k for k in range(8))
    assert normal.iloc[1]["volume"] == sum(1000 + k for k in range(8, 13))
    assert half.iloc[0]["volume"] == sum(1000 + k for k in range(7))       # 7 bars, one bucket
    assert f["complete"].all()


def test_buckets_never_span_two_sessions():
    raw = pd.concat([raw_30m(NORMAL_DAY, 13), raw_30m(HALF_DAY, 7)], ignore_index=True)
    for tf in ("1H", "4H"):
        f = to_frame(raw, tf, as_of=et(2026, 12, 1, 12), early_closes=frozenset({HALF_DAY}))
        days = f.index.tz_convert(MARKET_TZ).date
        assert len(set(days)) == 2 and list(days) == sorted(days)
    total = to_frame(raw, "4H", as_of=et(2026, 12, 1, 12), early_closes=frozenset({HALF_DAY}))["volume"].sum()
    assert total == raw["volume"].sum()


def test_half_day_last_bucket_completes_at_the_early_close_only_if_the_half_day_is_known():
    raw = raw_30m(HALF_DAY, 7)
    as_of = et(2026, 11, 27, 13, 5)
    known = to_frame(raw, "1H", as_of=as_of, early_closes=frozenset({HALF_DAY}))
    unknown = to_frame(raw, "1H", as_of=as_of)
    assert known["complete"].all()                              # 12:30 bucket ends at the 13:00 close
    assert list(unknown["complete"]) == [True, True, True, False]   # would wait for a 13:30 that never comes


def test_the_latest_bar_is_marked_incomplete():
    raw = raw_30m(NORMAL_DAY, 3)              # bars 09:30, 10:00, 10:30 fetched at 10:40
    h = to_frame(raw, "1H", as_of=et(2026, 11, 25, 10, 40))
    assert local(h.index) == ["09:30", "10:30"]
    assert list(h["complete"]) == [True, False]                 # 10:30 hour is only 10 minutes old
    assert local(drop_incomplete(h).index) == ["09:30"]


def test_a_partial_bar_frozen_in_the_cache_is_not_promoted_by_the_passage_of_time():
    """`as_of` is when the data was FETCHED, so it cannot be re-read later as complete."""
    raw = raw_30m(NORMAL_DAY, 3)
    stale = to_frame(raw, "1H", as_of=et(2026, 11, 25, 10, 40))     # fetched mid-bar
    assert not stale["complete"].iloc[-1]
    # the same bytes evaluated with a *later* as_of would say complete; the cache never does that
    assert to_frame(raw, "1H", as_of=et(2026, 11, 25, 16, 30))["complete"].iloc[-1]


def test_settle_delay_holds_back_a_bar_that_just_ended():
    raw = raw_30m(NORMAL_DAY, 2)                     # 09:30 and 10:00 -> one complete hour ending 10:30
    at = et(2026, 11, 25, 10, 30, )
    assert to_frame(raw, "1H", as_of=at)["complete"].iloc[0]
    assert not to_frame(raw, "1H", as_of=at, settle_seconds=60)["complete"].iloc[0]


def test_spring_forward_day_still_opens_at_0930_et():
    raw = raw_30m(date(2026, 3, 9), 4)               # Monday after DST began on Mar 8
    h = to_frame(raw, "1H", as_of=et(2026, 3, 9, 16, 30))
    assert h.index[0] == pd.Timestamp("2026-03-09 13:30", tz="UTC")     # 09:30 EDT = 13:30Z
    fri = to_frame(raw_30m(date(2026, 3, 6), 4), "1H", as_of=et(2026, 3, 9, 16, 30))
    assert fri.index[0] == pd.Timestamp("2026-03-06 14:30", tz="UTC")   # 09:30 EST = 14:30Z


def test_native_15m_passes_through():
    start = et(2026, 9, 18, 9, 30)
    raw = pd.DataFrame(
        {"datetime": [int((start + pd.Timedelta(minutes=15 * k)).timestamp() * 1000) for k in range(3)],
         "open": [1.0, 2, 3], "high": [2.0, 3, 4], "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5], "volume": [10, 20, 30]}
    )
    f = to_frame(raw, "15m", as_of=et(2026, 9, 18, 16, 30))
    assert local(f.index) == ["09:30", "09:45", "10:00"] and list(f["close"]) == [1.5, 2.5, 3.5]


def test_empty_input_gives_an_empty_frame():
    assert to_frame(pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), "1H",
                    as_of=et(2026, 9, 18, 16)).empty
    assert to_frame(pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), "1W",
                    as_of=et(2026, 9, 18, 16)).empty


def test_unknown_timeframe_is_rejected():
    with pytest.raises(ValueError):
        to_frame(raw_30m(NORMAL_DAY, 2), "2H", as_of=et(2026, 11, 25, 16))
