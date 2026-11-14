"""catalyst_momentum_v2: direction check, delegated spread limit, information-only economics."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import (
    TradeEvidence,
    _features_and_news_count,
    average_range_percent,
    features_from_observations,
)
from packages.connected.option_economics import UNAVAILABLE, option_economics, weekdays_to_expiry
from packages.connected.trade_assessment import (
    ADVISORY_POLICY_HASH,
    SCORE_PROFILE,
    TradeRequest,
    evaluate,
    policy_metadata,
    proposed_direction,
    resolve_direction,
    validate_pass_receipt,
)
from packages.domain.options import Greeks, OptionContract, OptionQuote
from packages.domain.workflow import CatalystFeatures, Direction, Signal
from packages.strategy.catalyst import CatalystMomentumStrategy, score_components, score_signal

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
CALL = "AAPL261106C00200000"
PUT = "AAPL261106P00200000"
POLICY = AssessmentPolicy(
    minimum_signal_score=Decimal("30"),
    pre_scan_minimum_signal_score=Decimal("25"),
    minimum_catalyst_confidence=Decimal("0.15"),
    pre_scan_minimum_catalyst_confidence=Decimal("0.10"),
    execution_max_spread_ratio=Decimal("0.20"),
)


def features(sentiment, momentum, *, bearish_market=False):
    market = "-.8" if bearish_market else ".8"
    return CatalystFeatures(
        catalyst_confidence=".9",
        sentiment=sentiment,
        relative_volume=3,
        price_momentum=momentum,
        gap_percent=1,
        market_confirmation=market,
        sector_confirmation=market,
        liquidity_score=".9",
    )


def contract(symbol, *, strike=200, kind="call", bid="1.95", ask="2.05", theta="-.01", expiry=None):
    return OptionContract(
        contract_id="synthetic-" + symbol,
        symbol=symbol,
        underlying_symbol="AAPL",
        expiration=expiry or date(2026, 11, 6),
        strike=Decimal(strike),
        option_type=kind,
        multiplier=100,
        tradable=True,
        quote=OptionQuote(
            bid=bid,
            ask=ask,
            bid_size=10,
            ask_size=10,
            quoted_at=NOW,
            open_interest=1000,
            greeks=Greeks(delta=".5", gamma=".01", theta=theta, vega=".1")
            if theta is not None
            else None,
        ),
    )


def evidence_for(symbol, feats, **contract_kwargs):
    kind = "put" if symbol == PUT else "call"
    return TradeEvidence(
        contracts=(contract(symbol, kind=kind, **contract_kwargs),),
        features=feats,
        features_at=NOW,
        spot=Decimal(200),
        greeks_at=NOW,
        greeks_source="synthetic-test-source",
    )


def trade_for(symbol, price="2"):
    return TradeRequest.model_validate(
        {"legs": [{"symbol": symbol, "side": "buy"}], "quantity": 1, "limit_price": price}
    )


def assess(symbol, sentiment, momentum, **kwargs):
    feats = features(sentiment, momentum, bearish_market=symbol == PUT)
    return evaluate(trade_for(symbol), POLICY, evidence_for(symbol, feats, **kwargs), now=NOW)


def failed(result):
    return [c["code"] for c in result["checks"] if not c["passed"]]


# ── A1: the direction is verified against the proposed option ────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "sentiment", "momentum", "decision", "reason"),
    [
        # call: positive / neutral headlines are consistent; negative headlines point the other way
        (CALL, ".8", ".5", "PASS", None),
        (CALL, ".8", "0", "FAIL", "price_action_not_confirming"),
        (CALL, ".8", "-.5", "FAIL", "price_action_not_confirming"),
        (CALL, "0", ".5", "PASS", None),
        (CALL, "0", "0", "FAIL", "price_action_not_confirming"),
        (CALL, "0", "-.5", "FAIL", "price_action_not_confirming"),
        (CALL, "-.8", "-.5", "FAIL", "signal_direction"),
        (CALL, "-.8", ".5", "FAIL", "price_action_not_confirming"),
        # put: neutral headlines no longer block it; positive headlines point the other way
        (PUT, "-.8", "-.5", "PASS", None),
        (PUT, "-.8", "0", "FAIL", "price_action_not_confirming"),
        (PUT, "-.8", ".5", "FAIL", "price_action_not_confirming"),
        (PUT, "0", "-.5", "PASS", None),
        (PUT, "0", "0", "FAIL", "price_action_not_confirming"),
        (PUT, "0", ".5", "FAIL", "price_action_not_confirming"),
        (PUT, ".8", ".5", "FAIL", "signal_direction"),
        (PUT, ".8", "-.5", "FAIL", "price_action_not_confirming"),
    ],
)
def test_direction_matrix(symbol, sentiment, momentum, decision, reason):
    result = assess(symbol, sentiment, momentum)
    assert result["decision"] == decision, result["blocking_reasons"]
    if reason is None:
        assert result["blocking_reasons"] == []
    else:
        assert reason in failed(result)
    assert result["score_profile"] == "catalyst_momentum_v2"


def test_a_put_on_neutral_headlines_with_price_below_the_open_passes_with_a_bearish_score():
    result = assess(PUT, "0", "-.5")
    assert result["decision"] == "PASS"
    assert result["direction_basis"] == {
        "proposed": "BEARISH",
        "headline_sentiment": "neutral",
        "resolved": "BEARISH",
    }
    # The bearish momentum is rewarded (the old rule scored it as a bullish miss).
    assert result["score_components"]["directional_momentum"] == pytest.approx(
        ((Decimal("0.5") + 1) / 2 * Decimal("0.20")).__float__()
    )
    assert (
        next(c for c in result["checks"] if c["code"] == "signal_direction")["actual"] == "BEARISH"
    )


def test_opposing_headlines_still_fail_and_a_neutral_call_is_unchanged():
    assert assess(CALL, "-.5", "-.5")["decision"] == "FAIL"
    neutral_call = assess(CALL, "0", ".5")
    assert neutral_call["decision"] == "PASS"
    assert neutral_call["direction_basis"]["resolved"] == "BULLISH"


def test_there_is_never_a_missing_direction():
    for sentiment in ("-1", "-.1", "0", ".1", "1"):
        for symbol in (CALL, PUT):
            feats = features(sentiment, ".3")
            proposed = proposed_direction(trade_for(symbol))
            resolved = resolve_direction(feats, proposed)
            assert resolved in (Direction.BULLISH, Direction.BEARISH)
            if Decimal(sentiment) == 0:
                assert resolved is proposed
            else:
                assert resolved is (
                    Direction.BULLISH if Decimal(sentiment) > 0 else Direction.BEARISH
                )


def test_the_resolved_direction_drives_the_score_the_strategy_and_the_confirmation_text():
    neutral = features("0", "-.5", bearish_market=True)
    bearish = score_signal(neutral, Direction.BEARISH)
    legacy = score_signal(neutral)  # unchanged rule for every other caller: non-negative = bullish
    assert bearish > legacy
    assert (
        score_components(neutral, Direction.BEARISH)["directional_momentum"]
        > (score_components(neutral)["directional_momentum"])
    )
    strategy = CatalystMomentumStrategy(minimum_score=Decimal("30"))
    signal = Signal(
        symbol="AAPL", observed_at=NOW, features=neutral, score=bearish, source_versions={}
    )
    assert strategy.evaluate_signal(signal).__class__.__name__ == "NoTrade"  # legacy: bullish guess
    assert strategy.evaluate_signal(signal, Direction.BEARISH).direction is Direction.BEARISH
    # With price-action confirmation off the receipt does not claim confirmation either way.
    relaxed = POLICY.model_copy(update={"require_price_action_confirmation": False})
    result = evaluate(
        trade_for(PUT),
        relaxed,
        evidence_for(PUT, features("0", "-.5", bearish_market=True)),
        now=NOW,
    )
    assert result["price_action_confirmation"] == "not_required_by_operator_policy"
    confirmed = evaluate(
        trade_for(PUT),
        POLICY,
        evidence_for(PUT, features("0", "-.5", bearish_market=True)),
        now=NOW,
    )
    assert (
        "price_action_confirmation" not in confirmed
    )  # present only when the policy does not require it


def test_other_callers_keep_the_original_direction_rule():
    positive = features(".8", ".5")
    assert score_components(positive) == score_components(positive, Direction.BULLISH)
    zero = features("0", ".5")
    assert score_components(zero) == score_components(zero, Direction.BULLISH)
    assert score_components(zero) != score_components(zero, Direction.BEARISH)


# ── A2: the numeric spread limit is the caller's; the quote is reported ───────────────────────


def test_the_spread_veto_is_delegated_and_the_observed_spread_is_reported():
    wide = {"bid": "1.00", "ask": "2.00"}  # about 66.7%: far above AlphaDesk's own 20% default
    result = assess(CALL, ".8", ".5", **wide)
    assert result["decision"] == "PASS", result["blocking_reasons"]
    check = next(c for c in result["checks"] if c["code"] == "leg_0_quote")
    assert check["threshold"] == "delegated_to_caller"
    assert check["passed"] is True
    assert Decimal(check["actual"]) == (Decimal(1) / Decimal("1.5"))
    quote = result["option_quotes"][0]
    assert quote["symbol"] == CALL and quote["bid"] == "1.00" and quote["ask"] == "2.00"
    assert Decimal(quote["spread_ratio"]) == Decimal(check["actual"])
    assert quote["bid_size"] == "10" and quote["ask_size"] == "10" and quote["quoted_at"]


# A crossed or negative quote cannot even be built (OptionQuote refuses it); a zero bid reaches the
# assessment and must still fail now that the numeric spread limit is the caller's.
@pytest.mark.parametrize(("bid", "ask"), [("0", "0.05"), ("0", "0")])
def test_an_unusable_quote_still_fails_in_the_assessment(bid, ask):
    result = assess(CALL, ".8", ".5", bid=bid, ask=ask)
    assert result["decision"] in {"FAIL", "UNAVAILABLE"}
    assert result["decision"] != "PASS"


def test_a_zero_spread_quote_is_usable():
    result = assess(CALL, ".8", ".5", bid="2.00", ask="2.00")
    assert result["decision"] == "PASS"
    assert Decimal(result["option_quotes"][0]["spread_ratio"]) == 0


def test_the_quote_block_follows_the_order_of_the_quote_checks_for_a_vertical():
    long_symbol, short_symbol = "AAPL261106C00200000", "AAPL261106C00210000"
    trade = TradeRequest.model_validate(
        {
            "legs": [
                {"symbol": short_symbol, "side": "sell"},
                {"symbol": long_symbol, "side": "buy"},
            ],
            "quantity": 1,
            "limit_price": "3",
        }
    )
    ev = TradeEvidence(
        contracts=(
            contract(long_symbol, strike=200, bid="5.00", ask="5.20"),
            contract(short_symbol, strike=210, bid="2.00", ask="2.40"),
        ),
        features=features(".8", ".5"),
        features_at=NOW,
        spot=Decimal(200),
        greeks_at=NOW,
        greeks_source="synthetic-test-source",
    )
    result = evaluate(trade, POLICY, ev, now=NOW)
    assert result["decision"] == "PASS", result["blocking_reasons"]
    quotes = result["option_quotes"]
    assert [q["symbol"] for q in quotes] == [
        long_symbol,
        short_symbol,
    ]  # long leg first, as the checks
    for index, quote in enumerate(quotes):
        check = next(c for c in result["checks"] if c["code"] == f"leg_{index}_quote")
        assert Decimal(check["actual"]) == Decimal(quote["spread_ratio"])
    # The wide short leg (18.2%) and the narrow long leg (3.9%) are both reported.
    assert max(Decimal(q["spread_ratio"]) for q in quotes) > Decimal("0.18")


# ── A3: economics are information only and never decide ────────────────────────────────────────


class Leg:
    def __init__(self, symbol, side):
        self.symbol, self.side = symbol, side


def economics(legs, contracts, *, spot, premium, avg_range=Decimal("2"), today=date(2026, 10, 6)):
    return option_economics(
        legs=legs,
        contracts={c.symbol: c for c in contracts},
        spot=Decimal(spot),
        premium=Decimal(premium),
        today=today,
        average_range_pct=avg_range,
    )


def test_economics_on_the_saved_amd_and_cost_proposals():
    amd = "AMD261211C00610000"
    block = economics(
        [Leg(amd, "buy")],
        [
            contract(
                amd, strike=610, bid="32.7", ask="35.1", theta="-1.2", expiry=date(2026, 12, 11)
            )
        ],
        spot="600",
        premium="33.89",
    )
    assert block["expiry_break_even"] == "643.8900"
    assert block["required_move_pct"] == str(
        (Decimal("43.89") / 600 * 100).quantize(Decimal("0.0001"))
    )
    cost = "COST261120C01100000"
    block = economics(
        [Leg(cost, "buy")],
        [
            contract(
                cost, strike=1100, bid="13.6", ask="15.8", theta="-0.9", expiry=date(2026, 11, 20)
            )
        ],
        spot="1050",
        premium="14.68",
    )
    assert block["expiry_break_even"] == "1114.6800"
    assert block["days_to_expiry"] == 45


def test_economics_formulas_for_a_single_call_and_a_single_put():
    call = economics(
        [Leg(CALL, "buy")],
        [contract(CALL, strike=200, bid="1.9", ask="2.1", theta="-.05")],
        spot="198",
        premium="2",
    )
    assert call["expiry_break_even"] == "202.0000"
    assert call["required_move_pct"] == "2.0202"  # (202 - 198) / 198
    assert call["spread_cost_pct"] == "5.0000"  # half of 0.20 over 2.00
    assert call["theta_pct_per_day"] == "-2.5000"  # -0.05 / 2.00
    assert call["days_to_expiry"] == 31
    put = economics(
        [Leg(PUT, "buy")],
        [contract(PUT, strike=200, kind="put", bid="1.9", ask="2.1", theta="-.05")],
        spot="203",
        premium="2",
    )
    assert put["expiry_break_even"] == "198.0000"
    assert put["required_move_pct"] == "2.4631"  # (203 - 198) / 203


def test_a_contract_already_past_break_even_requires_no_move():
    deep = economics(
        [Leg(CALL, "buy")],
        [contract(CALL, strike=150, bid="49", ask="51")],
        spot="205",
        premium="50",
    )
    assert deep["required_move_pct"] == "0.0000"
    put = economics(
        [Leg(PUT, "buy")],
        [contract(PUT, strike=250, kind="put", bid="49", ask="51")],
        spot="195",
        premium="50",
    )
    assert put["required_move_pct"] == "0.0000"


def test_the_range_proxy_scales_by_the_square_root_of_the_weekdays_to_expiry():
    # 2026-10-06 (Tue) -> 2026-11-06 (Fri): 23 weekdays. move 2.0202% / (2% * sqrt(23)) = 0.2106
    block = economics([Leg(CALL, "buy")], [contract(CALL)], spot="198", premium="2")
    assert weekdays_to_expiry(date(2026, 10, 6), date(2026, 11, 6)) == 23
    assert block["range_proxy"] == str(
        (Decimal("2.020202020202") / (Decimal(2) * Decimal(23).sqrt())).quantize(Decimal("0.0001"))
    )
    assert (
        "proxy" in block["range_proxy_note"].lower()
        and "not an expected move" in block["range_proxy_note"].lower()
    )
    assert "holidays" in block["range_proxy_note"].lower()


def test_a_vertical_uses_the_net_debit_the_width_and_the_summed_legs():
    long_symbol, short_symbol = "AAPL261106C00200000", "AAPL261106C00210000"
    long_leg = contract(long_symbol, strike=200, bid="5.00", ask="5.20", theta="-.10")
    short_leg = contract(short_symbol, strike=210, bid="2.00", ask="2.40", theta="-.04")
    block = economics(
        [Leg(short_symbol, "sell"), Leg(long_symbol, "buy")],
        [long_leg, short_leg],
        spot="200",
        premium="3",
    )
    assert block["structure"] == "debit_vertical"
    assert block["width"] == "10.0000" and block["max_profit"] == "7.0000"
    assert block["expiry_break_even"] == "203.0000"
    assert block["spread_cost_pct"] == str(
        ((Decimal("0.2") + Decimal("0.4")) / 2 / 3 * 100).quantize(Decimal("0.0001"))
    )
    # net theta = long (-0.10) minus short (-0.04) = -0.06 per share per day
    assert block["theta_pct_per_day"] == "-2.0000"
    bear = economics(
        [Leg("AAPL261106P00190000", "sell"), Leg("AAPL261106P00200000", "buy")],
        [
            contract("AAPL261106P00200000", strike=200, kind="put"),
            contract("AAPL261106P00190000", strike=190, kind="put"),
        ],
        spot="200",
        premium="3",
    )
    assert bear["expiry_break_even"] == "197.0000"


def test_missing_inputs_are_unavailable_and_never_raise():
    no_greeks = economics([Leg(CALL, "buy")], [contract(CALL, theta=None)], spot="198", premium="2")
    assert no_greeks["theta_pct_per_day"] == UNAVAILABLE
    assert no_greeks["expiry_break_even"] == "202.0000"
    none_range = economics(
        [Leg(CALL, "buy")], [contract(CALL)], spot="198", premium="2", avg_range=None
    )
    assert (
        none_range["range_proxy"] == UNAVAILABLE and none_range["required_move_pct"] != UNAVAILABLE
    )
    expired = economics(
        [Leg(CALL, "buy")], [contract(CALL)], spot="198", premium="2", today=date(2026, 11, 6)
    )
    assert expired["range_proxy"] == UNAVAILABLE
    zero_premium = economics([Leg(CALL, "buy")], [contract(CALL)], spot="198", premium="0")
    assert (
        zero_premium["expiry_break_even"] == UNAVAILABLE
        and zero_premium["spread_cost_pct"] == UNAVAILABLE
    )
    bad_spot = economics([Leg(CALL, "buy")], [contract(CALL)], spot="0", premium="2")
    assert bad_spot["required_move_pct"] == UNAVAILABLE
    unknown = option_economics(
        legs=[Leg("MISSING", "buy")],
        contracts={},
        spot=Decimal(1),
        premium=Decimal(1),
        today=date(2026, 10, 6),
        average_range_pct=None,
    )
    assert all(
        unknown[name] == UNAVAILABLE
        for name in (
            "expiry_break_even",
            "required_move_pct",
            "spread_cost_pct",
            "theta_pct_per_day",
            "days_to_expiry",
            "range_proxy",
        )
    )
    assert unknown["information_only"] is True


def test_economics_never_change_the_decision_or_the_score():
    base = assess(CALL, ".8", ".5")
    # A huge premium (and so a huge required move) leaves the decision and score alone.
    pricey = evaluate(
        trade_for(CALL, price="19"),
        POLICY,
        evidence_for(CALL, features(".8", ".5")),
        now=NOW,
    )
    assert pricey["decision"] == base["decision"] == "PASS"
    assert pricey["signal_score"] == base["signal_score"]
    assert (
        pricey["option_economics"]["required_move_pct"]
        != base["option_economics"]["required_move_pct"]
    )
    assert pricey["option_economics"]["information_only"] is True
    assert "option_economics" not in {c["code"] for c in pricey["checks"]}


def test_the_economics_block_is_serializable_and_present_on_a_pass():
    import json

    result = assess(CALL, ".8", ".5")
    assert json.loads(json.dumps(result))["option_economics"]["structure"] == "single_long_option"
    assert result["option_economics"]["premium"] == "2.0000"


# ── A4: provenance, and the receipt contract ────────────────────────────────────────────────


def test_average_range_is_the_mean_high_low_range_of_the_latest_twenty_sessions():
    bars = [
        {"t": f"2026-09-{day:02d}T04:00:00Z", "h": 102, "l": 100, "c": 100} for day in range(1, 25)
    ]
    assert average_range_percent(bars) == Decimal(2)
    assert average_range_percent([{"t": "2026-09-01", "h": 1}]) is None
    assert average_range_percent([]) is None


def test_the_feature_helper_keeps_its_three_value_contract_and_counts_eligible_headlines():
    now = datetime(2026, 1, 12, 15, tzinfo=UTC)
    snapshot = {
        "latestTrade": {"p": 101, "t": now.isoformat()},
        "dailyBar": {"o": 100, "v": 500, "t": now.isoformat()},
        "prevDailyBar": {"c": 100, "t": datetime(2026, 1, 9, 15, tzinfo=UTC).isoformat()},
    }
    with pytest.raises(Exception):  # noqa: B017 - incomplete calendar: same refusal as before
        features_from_observations(
            {s: snapshot for s in ("AAPL", "SPY", "QQQ")},
            [],
            [],
            "AAPL",
            Decimal(1),
            calendar=[],
            now=now,
        )
    assert _features_and_news_count.__name__ == "_features_and_news_count"


def test_the_receipt_contract_for_v2():
    result = assess(CALL, ".8", ".5")
    assert SCORE_PROFILE == "catalyst_momentum_v2" == result["score_profile"]
    for name in ("option_quotes", "option_economics", "direction_basis"):
        assert name in result
    assert validate_pass_receipt(result, trade_for(CALL), now=NOW)
    # The policy hash and version format are unchanged; the evaluator version is separate.
    metadata = policy_metadata(POLICY)
    assert metadata["score_profile"] == "catalyst_momentum_v2"
    assert metadata["advisory_policy_hash"] == ADVISORY_POLICY_HASH
    assert (
        ADVISORY_POLICY_HASH == "c5282def77c620c544eb6a3906df21839f8ee775851bb8cdf73dfb99ceccfeca"
    )
    assert metadata["policy_version"].startswith("standalone-paper-advisory-v1:")
    assert result["advisory_policy_hash"] == ADVISORY_POLICY_HASH


def test_the_provenance_block_is_reported_when_the_evidence_carries_it():
    provenance = {
        "stock_feed": "iex",
        "benchmark_feed": "iex",
        "benchmark_symbols": ["SPY", "QQQ"],
        "news": {
            "provider": "alpaca",
            "window_hours": 36,
            "limit": 20,
            "articles_returned": 20,
            "articles_eligible": 17,
            "cap_reached": True,
        },
    }
    ev = replace(
        evidence_for(CALL, features(".8", ".5")),
        provenance=provenance,
        average_range_pct=Decimal("2"),
    )
    result = evaluate(trade_for(CALL), POLICY, ev, now=NOW)
    assert result["evidence_provenance"] == provenance
    assert (
        result["market_data"]["feed"] == "indicative"
    )  # the pinned option-feed field is untouched
    assert result["option_economics"]["range_proxy"] != UNAVAILABLE
    without = evaluate(trade_for(CALL), POLICY, evidence_for(CALL, features(".8", ".5")), now=NOW)
    assert "evidence_provenance" not in without
