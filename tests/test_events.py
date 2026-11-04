"""Event detection, checked against sequences worked out by hand.

The main fixture (N=3, slow=5, K=3), bar by bar:

  i   o     h     l     c     v     entry channel (prior 3 bars)   outcome
  0   9    10     8     9    100
  1   9    11     9    10    100
  2  10    12    10    11    100
  3  11    12    10    11.5  100    U=12  L=8                      none (h=12 is not > 12)
  4  12    14    11    13.5  200    U=12  L=9                      LONG close  (13.5 > 12)
  5  13    13    11.5  12.5  100    U=14  L=10                     none
  6  13    15    12    14.5  300    U=14  L=10                     LONG close  (14.5 > 14)
  7  14    15.5   9.5  10.5  100    U=15  L=11                     SHORT close (10.5 < 11)
                                                                    LONG wick   (h 15.5 > 15, close 10.5 not)
"""

import numpy as np
import pandas as pd
import pytest

from donchian.channel import atr
from donchian.events import EVENT_COLUMNS, DetectorParams, detect
from tests.helpers import make_frame

P = DetectorParams(entry_n=3, slow_n=5, atr_n=3, volume_n=3, false_break_k=3)

SEQ = [
    (9, 10, 8, 9, 100),
    (9, 11, 9, 10, 100),
    (10, 12, 10, 11, 100),
    (11, 12, 10, 11.5, 100),
    (12, 14, 11, 13.5, 200),
    (13, 13, 11.5, 12.5, 100),
    (13, 15, 12, 14.5, 300),
    (14, 15.5, 9.5, 10.5, 100),
]


def ev(df, **kw):
    return detect(df, "TEST", P, **kw)


def pick(events, bar, direction, kind):
    row = events[(events["bar"] == bar) & (events["direction"] == direction) & (events["kind"] == kind)]
    assert len(row) == 1, f"expected exactly one {direction}/{kind} event on bar {bar}, got {len(row)}"
    return row.iloc[0]


def test_hand_computed_sequence_produces_exactly_these_events():
    e = ev(make_frame(SEQ))
    got = list(zip(e["bar"], e["direction"], e["kind"]))
    assert got == [(4, "long", "close"), (6, "long", "close"), (7, "long", "wick"), (7, "short", "close")]
    assert list(e.columns) == EVENT_COLUMNS
    assert (e["symbol"] == "TEST").all()


def test_close_event_fields_bar4():
    e = ev(make_frame(SEQ))
    r = pick(e, 4, "long", "close")
    assert r["entry_price"] == 13.5
    assert (r["prior_high"], r["prior_low"], r["channel_width"]) == (12.0, 9.0, 3.0)
    assert r["volume_ratio"] == pytest.approx(200 / 100)            # prior 3 bars all 100
    assert np.isnan(r["bars_since_prev"])                            # first long close
    assert not r["agrees_slow"]                                      # slow channel not warmed up yet


def test_atr_units_use_the_prior_bars_atr():
    df = make_frame(SEQ)
    prior_atr = atr(df["high"], df["low"], df["close"], 3).shift(1)
    e = ev(df)
    r = pick(e, 6, "long", "close")
    assert r["penetration_atr"] == pytest.approx((14.5 - 14.0) / prior_atr.iloc[6])
    assert r["width_atr"] == pytest.approx(4.0 / prior_atr.iloc[6])


def test_bars_since_previous_same_direction_and_kind():
    e = ev(make_frame(SEQ))
    assert pick(e, 6, "long", "close")["bars_since_prev"] == 2       # bar 4 -> bar 6
    assert np.isnan(pick(e, 7, "long", "wick")["bars_since_prev"])   # first long wick: not counted vs closes


def test_volume_vs_prior_average_excludes_the_current_bar():
    e = ev(make_frame(SEQ))
    # bar 6: prior 3 bars (3, 4, 5) = 100, 200, 100 -> mean 133.33; its own 300 is not in the mean
    assert pick(e, 6, "long", "close")["volume_ratio"] == pytest.approx(300 / (400 / 3))


def test_wick_only_touch_is_flagged_separately_from_close_confirmed():
    e = ev(make_frame(SEQ))
    wick = pick(e, 7, "long", "wick")
    assert wick["entry_price"] == 15.0                   # the level touched (open 14 was inside)
    assert wick["penetration_atr"] > 0
    short = pick(e, 7, "short", "close")
    assert short["entry_price"] == 10.5 and (short["prior_high"], short["prior_low"]) == (15.0, 11.0)
    # a close-confirmed breakout is never repeated as a wick event
    assert not ((e["bar"] == 4) & (e["kind"] == "wick")).any()


def test_slow_channel_agreement():
    e = ev(make_frame(SEQ))
    assert pick(e, 6, "long", "close")["agrees_slow"]       # 14.5 > slow upper 14
    assert pick(e, 7, "long", "wick")["agrees_slow"]        # high 15.5 > slow upper 15
    assert not pick(e, 7, "short", "close")["agrees_slow"]  # 10.5 is not below slow lower 10


def test_false_breakout_true_false_and_undecided():
    e = ev(make_frame(SEQ))
    assert bool(pick(e, 4, "long", "close")["false_breakout"]) is True     # bar 7 closed 10.5 <= 12
    assert bool(pick(e, 6, "long", "close")["false_breakout"]) is True     # bar 7 closed 10.5 <= 14
    assert pd.isna(pick(e, 7, "short", "close")["false_breakout"])   # no bars after it yet
    assert pd.isna(pick(e, 7, "long", "wick")["false_breakout"])


def _flat_then(rows_after, k=2):
    base = [(9.5, 10, 9, 9.5, 100)] * 3
    return make_frame(base + rows_after), DetectorParams(3, 5, 3, 3, k)


def test_false_breakout_false_when_k_complete_bars_hold_outside():
    df, p = _flat_then([(10.5, 11.5, 10.4, 11, 100), (11, 11.6, 10.9, 11.2, 100), (11.2, 11.8, 11, 11.5, 100)])
    r = detect(df, "T", p).iloc[0]
    assert (r["bar"], r["direction"], r["kind"]) == (3, "long", "close")
    assert bool(r["false_breakout"]) is False                    # closes 11.2, 11.5 both stay above 10


def test_false_breakout_undecided_until_k_bars_exist():
    df, p = _flat_then([(10.5, 11.5, 10.4, 11, 100), (11, 11.6, 10.9, 11.2, 100)])
    assert pd.isna(detect(df, "T", p).iloc[0]["false_breakout"])   # only 1 of 2 following bars


def test_false_breakout_true_as_soon_as_one_bar_closes_back_inside():
    df, p = _flat_then([(10.5, 11.5, 10.4, 11, 100), (10.5, 10.6, 9.6, 9.9, 100)], k=3)
    assert bool(detect(df, "T", p).iloc[0]["false_breakout"]) is True    # closed 9.9 <= 10 with K=3 not yet elapsed


def test_flat_series_has_no_signal():
    rows = [(10, 10, 10, 10, 100)] * 40
    e = ev(make_frame(rows))
    assert e.empty and list(e.columns) == EVENT_COLUMNS


def test_fewer_bars_than_n_has_no_signal():
    e = detect(make_frame(SEQ[:3]), "T", DetectorParams(5, 8, 3, 3, 3))
    assert e.empty
    # even a huge last bar cannot break out when there are not N prior bars
    rows = [(10, 10.2, 9.8, 10, 100)] * 4 + [(10, 50, 10, 50, 100)]
    assert detect(make_frame(rows), "T", DetectorParams(5, 8, 3, 3, 3)).empty


def test_gap_up_through_channel_closing_high_is_a_close_event():
    rows = [(9.5, 10, 9, 9.5, 100)] * 3 + [(12, 13, 12, 12.5, 100)]
    r = detect(make_frame(rows), "T", DetectorParams(3, 5, 3, 3, 3)).iloc[0]
    assert (r["direction"], r["kind"]) == ("long", "close")
    assert r["entry_price"] == 12.5 and r["prior_high"] == 10.0


def test_gap_up_that_fades_back_is_a_wick_event_entered_at_the_open():
    rows = [(9.5, 10, 9, 9.5, 100)] * 3 + [(12, 13, 9.5, 10, 100)]   # gapped to 12, closed 10 (not > 10)
    r = detect(make_frame(rows), "T", DetectorParams(3, 5, 3, 3, 3)).iloc[0]
    assert (r["direction"], r["kind"]) == ("long", "wick")
    assert r["entry_price"] == 12.0            # you could not have been filled at the 10 level


def test_close_exactly_on_the_channel_is_not_a_breakout():
    rows = [(9.5, 10, 9, 9.5, 100)] * 3 + [(9.5, 10, 9, 10, 100)]
    assert detect(make_frame(rows), "T", DetectorParams(3, 5, 3, 3, 3)).empty      # strict inequality


def test_a_forming_bar_never_signals_unless_asked():
    complete = [True] * 3 + [False]
    df = make_frame([(9.5, 10, 9, 9.5, 100)] * 3 + [(10, 12, 10, 11.5, 100)], complete=complete)
    p = DetectorParams(3, 5, 3, 3, 3)
    assert detect(df, "T", p).empty
    r = detect(df, "T", p, include_incomplete=True).iloc[0]
    assert r["bar"] == 3 and not r["complete"]


def test_incomplete_following_bars_do_not_decide_false_breakout():
    complete = [True] * 4 + [False]
    df = make_frame(
        [(9.5, 10, 9, 9.5, 100)] * 3 + [(10.5, 11.5, 10.4, 11, 100), (10.5, 10.6, 9.6, 9.9, 100)],
        complete=complete,
    )
    r = detect(df, "T", DetectorParams(3, 5, 3, 3, 3)).iloc[0]
    assert pd.isna(r["false_breakout"])        # the only follower is still forming: undecided, not True
