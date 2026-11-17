"""Phase B: AlphaDesk assesses all eight engine structures (dark release).

Layers: L1 engine maths at several quantities, L2 shape matrix and gate, L3 per-structure assessment
(direction, each credit/protective check, evidence failures), plus policy and wire-compat guards.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import TradeEvidence
from packages.connected.trade_assessment import (
    PROTECTIVE_PROFILE,
    TradeRequest,
    evaluate,
    policy_metadata,
    proposal_fingerprint,
    trade_wire,
    validate_pass_receipt,
)
from packages.connected.trade_structure import classify_shape, gate_structure
from packages.domain.options import (
    Greeks,
    LegSide,
    OptionContract,
    OptionLeg,
    OptionQuote,
    OptionType,
    StructureType,
    UnderlyingLeg,
)
from packages.domain.workflow import CatalystFeatures
from packages.options.engine import build_structure, expiration_payoff

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
EXPIRY = date(2026, 11, 6)
ALL_EIGHT = [member.value for member in StructureType]
FOUR_NEW = [
    "bull_put_credit_spread",
    "bear_call_credit_spread",
    "protective_put",
    "protective_put_spread",
]
OLD_FOUR = ["long_call", "long_put", "bull_call_debit_spread", "bear_put_debit_spread"]


def sym(kind: str, strike: int, *, expiry: str = "261106", root: str = "AAPL") -> str:
    return f"{root}{expiry}{kind}{strike * 1000:08d}"


def contract(kind: str, strike: int, bid: str, ask: str, delta: str, **over) -> OptionContract:
    quote = OptionQuote(
        bid=bid,
        ask=ask,
        bid_size=over.pop("bid_size", 10),
        ask_size=10,
        quoted_at=over.pop("quoted_at", NOW),
        open_interest=over.pop("open_interest", 1000),
        greeks=over.pop("greeks", Greeks(delta=delta, gamma=".01", theta="-.01", vega=".1")),
    )
    return OptionContract(
        contract_id=f"fixture-{kind}{strike}",
        symbol=sym(kind, strike),
        underlying_symbol="AAPL",
        expiration=over.pop("expiration", EXPIRY),
        strike=Decimal(strike),
        option_type=OptionType.CALL if kind == "C" else OptionType.PUT,
        multiplier=100,
        tradable=True,
        quote=quote,
    )


def features(*, bullish: bool = True, sentiment: str | None = None, confidence: str = ".9"):
    sign = 1 if bullish else -1
    return CatalystFeatures(
        catalyst_confidence=confidence,
        sentiment=sentiment if sentiment is not None else str(Decimal(".8") * sign),
        relative_volume=3,
        price_momentum=str(Decimal(".8") * sign),
        gap_percent=1,
        market_confirmation=".8",
        sector_confirmation=".8",
        liquidity_score=".9",
    )


def evidence(contracts, *, bullish=True, spot="200", **kw) -> TradeEvidence:
    return TradeEvidence(
        contracts=tuple(contracts),
        features=kw.pop("features", features(bullish=bullish)),
        features_at=NOW,
        spot=Decimal(spot),
        greeks_at=NOW,
        greeks_source="synthetic-test-source",
        **kw,
    )


C200, C205, C210 = (
    contract("C", 200, "1.95", "2.05", ".5"),
    contract("C", 205, "2.50", "2.60", ".28"),
    contract("C", 210, "1.10", "1.20", ".15"),
)
P190, P195, P185, P200 = (
    contract("P", 190, "1.10", "1.20", "-.15"),
    contract("P", 195, "2.50", "2.60", "-.28"),
    contract("P", 185, "1.10", "1.20", "-.10"),
    contract("P", 200, "1.95", "2.05", "-.5"),
)
HEDGE_PUT = contract("P", 195, "3.00", "3.10", "-.45")
STOCK = {"symbol": "AAPL", "shares": 100, "entry_price": "195"}


def request(legs, limit="1.30", quantity=1, stock=None, ref="t1") -> TradeRequest:
    body = {
        "legs": [{"symbol": s, "side": side} for s, side in legs],
        "quantity": quantity,
        "limit_price": limit,
        "client_reference": ref,
    }
    if stock is not None:
        body["stock_context"] = stock
    return TradeRequest.model_validate(body)


def policy(allowed=None, **kw) -> AssessmentPolicy:
    return AssessmentPolicy(allowed_structures=allowed or ALL_EIGHT, **kw)


BULL_PUT = [(P190.symbol, "buy"), (P195.symbol, "sell")]
BEAR_CALL = [(C210.symbol, "buy"), (C205.symbol, "sell")]
BULL_PUT_CONTRACTS, BEAR_CALL_CONTRACTS = (P190, P195), (C205, C210)
PROT_PUT = [(HEDGE_PUT.symbol, "buy")]
PROT_SPREAD = [(HEDGE_PUT.symbol, "buy"), (P185.symbol, "sell")]
PROT_SPREAD_CONTRACTS = (HEDGE_PUT, P185)


def check_of(result, code):
    return next(c for c in result["checks"] if c["code"] == code)


# ---------------------------------------------------------------- L1: engine maths, all eight
def build_all8(quantity: int):
    def leg(kind, strike, side, price):
        c = contract(kind, strike, "1", "2", ".1")
        return OptionLeg(contract=c, side=side, entry_price=Decimal(price))

    shares = 100 * quantity
    stock = UnderlyingLeg(symbol="AAPL", shares=shares, entry_price=Decimal("100"))
    long_, short_ = LegSide.LONG, LegSide.SHORT
    return {
        StructureType.LONG_CALL: ((leg("C", 100, long_, "3"),), None),
        StructureType.LONG_PUT: ((leg("P", 100, long_, "3"),), None),
        StructureType.BULL_CALL_DEBIT_SPREAD: (
            (leg("C", 100, long_, "5"), leg("C", 110, short_, "2")),
            None,
        ),
        StructureType.BEAR_PUT_DEBIT_SPREAD: (
            (leg("P", 110, long_, "5"), leg("P", 100, short_, "2")),
            None,
        ),
        StructureType.BULL_PUT_CREDIT_SPREAD: (
            (leg("P", 90, long_, "1"), leg("P", 100, short_, "3")),
            None,
        ),
        StructureType.BEAR_CALL_CREDIT_SPREAD: (
            (leg("C", 110, long_, "1"), leg("C", 100, short_, "3")),
            None,
        ),
        StructureType.PROTECTIVE_PUT: ((leg("P", 95, long_, "2"),), stock),
        StructureType.PROTECTIVE_PUT_SPREAD: (
            (leg("P", 95, long_, "3"), leg("P", 85, short_, "1")),
            stock,
        ),
    }


@pytest.mark.parametrize("quantity", [1, 3, 10])
def test_engine_payoff_sweep_matches_declared_bounds_for_all_eight(quantity):
    for structure_type, (legs, stock) in build_all8(quantity).items():
        s = build_structure(structure_type, legs, quantity=quantity, underlying=stock)
        grid = [Decimal(p) for p in range(0, 301, 1)]
        payoffs = [expiration_payoff(s, price) for price in grid]
        assert min(payoffs) >= -s.max_loss, structure_type
        if s.max_profit is not None:
            assert max(payoffs) <= s.max_profit, structure_type
        assert expiration_payoff(s, s.break_evens[0]) == 0, structure_type
        one = build_structure(
            structure_type, legs, quantity=1, underlying=build_all8(1)[structure_type][1]
        )
        assert s.max_loss == one.max_loss * quantity, structure_type  # linear in quantity


def test_protective_put_spread_is_not_a_permanent_floor_below_the_short_strike():
    legs, stock = build_all8(1)[StructureType.PROTECTIVE_PUT_SPREAD]
    s = build_structure(StructureType.PROTECTIVE_PUT_SPREAD, legs, underlying=stock)
    floor_region = [expiration_payoff(s, Decimal(p)) for p in (80, 60, 40, 0)]
    assert floor_region == sorted(floor_region, reverse=True)  # loss keeps growing below 85
    assert floor_region[-1] < floor_region[0]


# ---------------------------------------------------------------- L2: shape matrix and gate
CLASSIFY_CASES = [
    ([(C200.symbol, "buy")], None, StructureType.LONG_CALL),
    ([(P200.symbol, "buy")], None, StructureType.LONG_PUT),
    ([(C200.symbol, "buy"), (C205.symbol, "sell")], None, StructureType.BULL_CALL_DEBIT_SPREAD),
    ([(C205.symbol, "sell"), (C200.symbol, "buy")], None, StructureType.BULL_CALL_DEBIT_SPREAD),
    ([(P200.symbol, "buy"), (P195.symbol, "sell")], None, StructureType.BEAR_PUT_DEBIT_SPREAD),
    ([(P195.symbol, "sell"), (P200.symbol, "buy")], None, StructureType.BEAR_PUT_DEBIT_SPREAD),
    (BULL_PUT, None, StructureType.BULL_PUT_CREDIT_SPREAD),
    (list(reversed(BULL_PUT)), None, StructureType.BULL_PUT_CREDIT_SPREAD),
    (BEAR_CALL, None, StructureType.BEAR_CALL_CREDIT_SPREAD),
    (list(reversed(BEAR_CALL)), None, StructureType.BEAR_CALL_CREDIT_SPREAD),
    (PROT_PUT, STOCK, StructureType.PROTECTIVE_PUT),
    (PROT_SPREAD, STOCK, StructureType.PROTECTIVE_PUT_SPREAD),
    (list(reversed(PROT_SPREAD)), STOCK, StructureType.PROTECTIVE_PUT_SPREAD),
]


@pytest.mark.parametrize("legs,stock,expected", CLASSIFY_CASES)
def test_classifier_recognises_each_structure_in_both_leg_orders(legs, stock, expected):
    assert classify_shape(request(legs, stock=stock)) is expected


def other_expiry(kind, strike):
    return sym(kind, strike, expiry="261113")


MALFORMED = [
    ("naked short call", [(C205.symbol, "sell")], None),
    ("naked short put", [(P195.symbol, "sell")], None),
    ("same symbol twice", [(C200.symbol, "buy"), (C200.symbol, "sell")], None),
    ("two longs", [(C200.symbol, "buy"), (C205.symbol, "buy")], None),
    ("two shorts", [(C200.symbol, "sell"), (C205.symbol, "sell")], None),
    ("mixed expiry", [(C200.symbol, "buy"), (other_expiry("C", 205), "sell")], None),
    ("mixed type", [(C200.symbol, "buy"), (P195.symbol, "sell")], None),
    ("straddle", [(C200.symbol, "buy"), (P200.symbol, "buy")], None),
    ("different roots", [(C200.symbol, "buy"), (sym("C", 205, root="MSFT"), "sell")], None),
    ("stock on long call", [(C200.symbol, "buy")], STOCK),
    ("stock on bull call debit", [(C200.symbol, "buy"), (C205.symbol, "sell")], STOCK),
    ("stock on bull put credit", BULL_PUT, STOCK),
    ("stock on bear call credit", BEAR_CALL, STOCK),
    ("protective with a sold put", [(P195.symbol, "sell")], STOCK),
    ("protective with wrong-way spread", [(P195.symbol, "buy"), (P200.symbol, "sell")], STOCK),
]


@pytest.mark.parametrize("name,legs,stock", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_shapes_are_not_classified(name, legs, stock):
    assert classify_shape(request(legs, stock=stock)) is None


def test_ratio_other_than_one_is_not_classified():
    body = {
        "legs": [
            {"symbol": C200.symbol, "side": "buy", "ratio_quantity": 2},
            {"symbol": C205.symbol, "side": "sell"},
        ],
        "quantity": 1,
        "limit_price": "1",
    }
    assert classify_shape(TradeRequest.model_validate(body)) is None


def test_default_allowlist_keeps_the_four_new_shapes_dark_with_todays_exact_unsupported_receipt():
    default = AssessmentPolicy()
    for legs, stock, expected in CLASSIFY_CASES:
        trade = request(legs, stock=stock)
        gated = gate_structure(trade, default)
        assert (gated is not None) == (expected.value in OLD_FOUR), expected
    for legs, stock in (
        (BULL_PUT, None),
        (BEAR_CALL, None),
        (PROT_PUT, STOCK),
        (PROT_SPREAD, STOCK),
    ):
        result = evaluate(request(legs, stock=stock), default, None, now=NOW)
        assert result["decision"] == "UNAVAILABLE"
        assert result["blocking_reasons"] == ["unsupported_strategy"]
        assert result["retryable"] is False
        assert result["remediation"] == [
            "Submit a long call/put or same-expiry 1:1 directional debit vertical. "
            "Naked sales and other structures are unsupported."
        ]


def test_enabling_a_subset_enables_only_that_subset():
    only_credit = policy(allowed=["bull_put_credit_spread"])
    assert gate_structure(request(BULL_PUT), only_credit) is StructureType.BULL_PUT_CREDIT_SPREAD
    assert gate_structure(request(BEAR_CALL), only_credit) is None
    assert gate_structure(request([(C200.symbol, "buy")]), only_credit) is None


# ---------------------------------------------------------------- L3: credit structures
def pass_credit(legs=BULL_PUT, contracts=BULL_PUT_CONTRACTS, bullish=True, **kw):
    return evaluate(request(legs), policy(), evidence(contracts, bullish=bullish, **kw), now=NOW)


@pytest.mark.parametrize(
    "legs,contracts,supportive",
    [(BULL_PUT, BULL_PUT_CONTRACTS, True), (BEAR_CALL, BEAR_CALL_CONTRACTS, False)],
)
def test_credit_pass_carries_structure_numbers_and_never_an_iv_check(legs, contracts, supportive):
    for order in (legs, list(reversed(legs))):
        result = evaluate(
            request(order), policy(), evidence(contracts, bullish=supportive), now=NOW
        )
        assert result["decision"] == "PASS", result["remediation"]
        assert result["price_kind"] == "credit"
        assert result["max_profit"] == "130.00" or Decimal(result["max_profit"]) == Decimal("130")
        assert Decimal(result["max_loss"]) == Decimal("370")
        assert result["iv_edge"] == "unavailable"
        assert not any("iv" in c["code"] for c in result["checks"])
        assert result["option_economics"]["required_move_pct"] == "not_applicable"
        assert result["effective_dte_window"] == "14..45"
        assert "assignment_note" in result
        codes = {c["code"] for c in result["checks"]}
        assert {
            "credit_below_width",
            "credit_to_width_minimum",
            "short_leg_delta_maximum",
            "short_strike_out_of_the_money",
            "credit_dte_window",
        } <= codes
        assert all(c["passed"] for c in result["checks"])


@pytest.mark.parametrize(
    "legs,contracts,supportive,opposing_label",
    [
        (BULL_PUT, BULL_PUT_CONTRACTS, True, "BEARISH"),
        (BEAR_CALL, BEAR_CALL_CONTRACTS, False, "BULLISH"),
    ],
)
def test_credit_direction_check_uses_the_structure_not_the_first_leg(
    legs, contracts, supportive, opposing_label
):
    # Opposing evidence: the nested signal_direction check follows the structure's direction.
    for order in (legs, list(reversed(legs))):
        bad = evaluate(
            request(order), policy(), evidence(contracts, bullish=not supportive), now=NOW
        )
        direction = check_of(bad, "signal_direction")
        assert direction["passed"] is False
        assert direction["actual"] == opposing_label
        assert direction["threshold"] == ("BULLISH" if supportive else "BEARISH")
        assert bad["decision"] == "FAIL"
        good = evaluate(request(order), policy(), evidence(contracts, bullish=supportive), now=NOW)
        assert check_of(good, "signal_direction")["passed"] is True
        assert good["direction_basis"]["proposed"] == ("BULLISH" if supportive else "BEARISH")


def test_neutral_headline_defers_to_the_structure_direction():
    neutral = features(bullish=True, sentiment="0")
    ok = evaluate(
        request(BULL_PUT), policy(), evidence(BULL_PUT_CONTRACTS, features=neutral), now=NOW
    )
    assert ok["direction_basis"] == {
        "proposed": "BULLISH",
        "headline_sentiment": "neutral",
        "resolved": "BULLISH",
    }
    assert check_of(ok, "signal_direction")["passed"] is True


def test_each_credit_check_can_fail_on_its_own():
    thin = evaluate(
        request(BULL_PUT, limit="1.00"), policy(), evidence(BULL_PUT_CONTRACTS), now=NOW
    )
    assert check_of(thin, "credit_to_width_minimum")["passed"] is False
    too_wide = evaluate(
        request(BULL_PUT, limit="5"), policy(), evidence(BULL_PUT_CONTRACTS), now=NOW
    )
    assert check_of(too_wide, "credit_below_width")["passed"] is False
    heavy_delta = contract("P", 195, "2.50", "2.60", "-.45")
    high_delta = evaluate(request(BULL_PUT), policy(), evidence((P190, heavy_delta)), now=NOW)
    assert check_of(high_delta, "short_leg_delta_maximum")["passed"] is False
    itm = evaluate(request(BULL_PUT), policy(), evidence(BULL_PUT_CONTRACTS, spot="190"), now=NOW)
    assert check_of(itm, "short_strike_out_of_the_money")["passed"] is False
    soon = date(2026, 10, 12)
    near = [replace_expiry(P190, soon), replace_expiry(P195, soon)]
    near_trade = request([(near[0].symbol, "buy"), (near[1].symbol, "sell")])
    early = evaluate(near_trade, policy(), evidence(near), now=NOW)
    assert check_of(early, "credit_dte_window")["passed"] is False


def replace_expiry(c: OptionContract, expiry: date) -> OptionContract:
    new_symbol = c.symbol.replace("261106", expiry.strftime("%y%m%d"))
    return c.model_copy(update={"expiration": expiry, "symbol": new_symbol})


def test_credit_dte_window_is_the_intersection_with_the_workspace_window():
    narrowed = policy(minimum_dte=20, maximum_dte=40, credit_dte_min=25, credit_dte_max=60)
    result = evaluate(request(BULL_PUT), narrowed, evidence(BULL_PUT_CONTRACTS), now=NOW)
    assert result["effective_dte_window"] == "25..40"
    assert check_of(result, "credit_dte_window")["threshold"] == "25..40"


@pytest.mark.parametrize(
    "mutate,code,decision",
    [
        (
            lambda c: c.model_copy(
                update={
                    "quote": c.quote.model_copy(
                        update={"quoted_at": datetime(2026, 10, 6, 14, 0, tzinfo=UTC)}
                    )
                }
            ),
            "stale_or_invalid_evidence",
            "UNAVAILABLE",
        ),
        (
            lambda c: c.model_copy(update={"quote": c.quote.model_copy(update={"greeks": None})}),
            "option_greeks_missing",
            "UNAVAILABLE",
        ),
        (
            lambda c: c.model_copy(
                update={"quote": c.quote.model_copy(update={"open_interest": None})}
            ),
            "open_interest_missing",
            "UNAVAILABLE",
        ),
        (
            lambda c: c.model_copy(
                update={"quote": c.quote.model_copy(update={"bid_size": Decimal(0)})}
            ),
            "quote_depth",
            "FAIL",
        ),
    ],
)
def test_credit_evidence_failures_are_distinct_from_quality_failures(mutate, code, decision):
    broken = (P190, mutate(P195))
    result = evaluate(request(BULL_PUT), policy(), evidence(broken), now=NOW)
    assert result["decision"] == decision
    assert code in result["blocking_reasons"] or result["blocking_reasons"] == [code]


# ---------------------------------------------------------------- L3: protective structures
@pytest.mark.parametrize(
    "legs,contracts,limit",
    [(PROT_PUT, (HEDGE_PUT,), "3.05"), (PROT_SPREAD, PROT_SPREAD_CONTRACTS, "2.00")],
)
def test_protective_passes_without_any_catalyst_veto_even_on_bearish_or_no_news(
    legs, contracts, limit
):
    for ev_features in (
        features(bullish=False),
        features(bullish=True, sentiment="0", confidence="0.01"),
    ):
        ev = evidence(contracts, features=ev_features)
        result = evaluate(request(legs, limit=limit, stock=STOCK), policy(), ev, now=NOW)
        assert result["decision"] == "PASS", result["remediation"]
        assert result["signal_score"] is None and result["score_components"] is None
        assert result["score_profile"] == PROTECTIVE_PROFILE
        assert result["hedge_appropriateness"] == "delegated_to_caller"
        assert "direction_basis" not in result and "catalyst_confidence" not in result
        codes = {c["code"] for c in result["checks"]}
        assert not codes & {"signal_direction", "minimum_signal_score", "direct_signal_evidence"}
        assert {
            "shares_match_contracts",
            "stock_symbol_matches",
            "protective_cost_maximum",
            "put_floor_below_spot",
        } <= codes
        assert result["trade"]["stock_context"] == {
            "symbol": "AAPL",
            "shares": 100,
            "entry_price": "195",
        }
        assert result["trade_fingerprint"] == proposal_fingerprint(
            request(legs, limit=limit, stock=STOCK)
        )
        assert result["price_kind"] == "debit" and "max_loss" in result
        # AD's own MCP validator must refuse this profile, so it is never presented as usable there.
        assert (
            validate_pass_receipt(result, request(legs, limit=limit, stock=STOCK), now=NOW) is False
        )


def test_protective_checks_fail_individually():
    def base(**kw):
        return evaluate(
            request(
                PROT_PUT,
                limit=kw.get("limit", "3.05"),
                quantity=kw.get("qty", 1),
                stock=kw.get("stock", STOCK),
            ),
            policy(),
            evidence((HEDGE_PUT,), spot=kw.get("spot", "200")),
            now=NOW,
        )

    assert (
        check_of(base(stock={**STOCK, "shares": 150}), "shares_match_contracts")["passed"] is False
    )
    assert (
        check_of(base(stock={**STOCK, "symbol": "MSFT"}), "stock_symbol_matches")["passed"] is False
    )
    assert check_of(base(limit="12"), "protective_cost_maximum")["passed"] is False
    assert check_of(base(spot="190"), "put_floor_below_spot")["passed"] is False
    wide = evaluate(
        request(PROT_SPREAD, limit="10", stock=STOCK),
        policy(),
        evidence(PROT_SPREAD_CONTRACTS),
        now=NOW,
    )
    assert check_of(wide, "debit_below_width")["passed"] is False


def with_quote(c, **changes):
    return c.model_copy(update={"quote": c.quote.model_copy(update=changes)})


PROTECTIVE_FORMS = [
    ("protective put", PROT_PUT, (HEDGE_PUT,), "3.05"),
    ("protective put spread", PROT_SPREAD, PROT_SPREAD_CONTRACTS, "2.00"),
]
# (name, build contracts from the healthy ones, evidence kwargs, expected reason)
EARLY_FAILURES = [
    (
        "stale quote",
        lambda cs: (with_quote(cs[0], quoted_at=datetime(2026, 10, 6, 14, 0, tzinfo=UTC)), *cs[1:]),
        {},
        "stale_or_invalid_evidence",
    ),
    (
        "future quote",
        lambda cs: (with_quote(cs[0], quoted_at=datetime(2026, 10, 6, 16, 0, tzinfo=UTC)), *cs[1:]),
        {},
        "stale_or_invalid_evidence",
    ),
    (
        "missing contract evidence",
        lambda cs: cs[:1] if len(cs) > 1 else (P185,),
        {},
        "contract_evidence_missing",
    ),
    (
        "contract identity mismatch",
        lambda cs: (cs[0].model_copy(update={"strike": Decimal(196)}), *cs[1:]),
        {},
        "contract_identity_mismatch",
    ),
    ("missing spot", lambda cs: cs, {"spot": "0"}, "underlying_price_missing"),
    (
        "missing greeks",
        lambda cs: (
            (with_quote(cs[-1], greeks=None), *cs[:-1])
            if len(cs) > 1
            else (with_quote(cs[0], greeks=None),)
        ),
        {},
        "option_greeks_missing",
    ),
    (
        "missing open interest",
        lambda cs: (with_quote(cs[0], open_interest=None), *cs[1:]),
        {},
        "open_interest_missing",
    ),
    (
        "zero bid depth on the first leg",
        lambda cs: (with_quote(cs[0], bid_size=Decimal(0)), *cs[1:]),
        {},
        "quote_depth",
    ),
    (
        "zero ask depth on the last leg",
        lambda cs: (*cs[:-1], with_quote(cs[-1], ask_size=Decimal(0))),
        {},
        "quote_depth",
    ),
]


def assert_protective_contract(result, reason):
    assert result["decision"] == "UNAVAILABLE", result["blocking_reasons"]
    assert result["blocking_reasons"] == [reason]
    assert result["score_profile"] == PROTECTIVE_PROFILE
    assert result["signal_score"] is None and result["score_components"] is None
    assert result["minimum_passing_score"] is None
    assert result["hedge_appropriateness"] == "delegated_to_caller"
    assert "not verified" in result["stock_context_basis"]
    assert "catalyst_confidence" not in result and "direction_basis" not in result


@pytest.mark.parametrize(
    "form,legs,contracts,limit", PROTECTIVE_FORMS, ids=[f[0] for f in PROTECTIVE_FORMS]
)
@pytest.mark.parametrize(
    "name,mutate,ev_kwargs,reason", EARLY_FAILURES, ids=[e[0] for e in EARLY_FAILURES]
)
def test_every_enabled_protective_evidence_failure_keeps_the_protective_contract(
    form, legs, contracts, limit, name, mutate, ev_kwargs, reason
):
    if name == "missing contract evidence" and len(contracts) == 1:
        broken = (P185,)  # evidence for a different contract than the one requested
    else:
        broken = tuple(mutate(contracts))
    result = evaluate(
        request(legs, limit=limit, stock=STOCK), policy(), evidence(broken, **ev_kwargs), now=NOW
    )
    assert_protective_contract(result, reason)
    # Absent evidence is retryable; it never became a FAIL or a catalyst-scored receipt.
    assert result["retryable"] is True


def test_greek_provenance_contradiction_keeps_the_protective_contract():
    ev = evidence((HEDGE_PUT,))
    ev = replace(ev, greeks_at=None)  # a source without a time
    result = evaluate(request(PROT_PUT, limit="3.05", stock=STOCK), policy(), ev, now=NOW)
    assert_protective_contract(result, "greek_provenance_contradictory")


def test_genuine_protective_quality_failures_are_still_fail_not_unavailable():
    pricey = evaluate(
        request(PROT_PUT, limit="12", stock=STOCK), policy(), evidence((HEDGE_PUT,)), now=NOW
    )
    assert pricey["decision"] == "FAIL" and pricey["score_profile"] == PROTECTIVE_PROFILE


def test_disabled_protective_requests_keep_todays_exact_unsupported_receipt():
    for legs, limit in ((PROT_PUT, "3.05"), (PROT_SPREAD, "2.00")):
        result = evaluate(
            request(legs, limit=limit, stock=STOCK), AssessmentPolicy(), None, now=NOW
        )
        assert result["score_profile"] == "catalyst_momentum_v2"
        assert result["minimum_passing_score"] == "65"
        assert "hedge_appropriateness" not in result and "stock_context_basis" not in result


def test_stock_context_validation_and_the_v3_fingerprint_contract():
    for bad in (
        {**STOCK, "shares": 0},
        {**STOCK, "entry_price": "0"},
        {**STOCK, "symbol": "aapl"},
        {**STOCK, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            request(PROT_PUT, limit="3.05", stock=bad)
    a = request(PROT_PUT, limit="3.05", stock=STOCK)
    # Frozen v3 encoding: the v2 canonical string with the prefix changed and the stock appended.
    import hashlib

    expected = hashlib.sha256(
        (
            "standalone-proposal-v3|q:1|p:3.05|r:2:t1|n:1|"
            f"{len(HEDGE_PUT.symbol)}:{HEDGE_PUT.symbol}:buy:1|s:4:AAPL:100:195"
        ).encode()
    ).hexdigest()
    assert proposal_fingerprint(a) == expected
    for changed in (
        request(PROT_PUT, limit="3.05", stock={**STOCK, "shares": 200}),
        request(PROT_PUT, limit="3.05", stock={**STOCK, "entry_price": "196"}),
        request(PROT_PUT, limit="3.05", stock={**STOCK, "symbol": "MSFT"}),
    ):
        assert proposal_fingerprint(changed) != expected


# ---------------------------------------------- wire compatibility of the existing shapes
def test_existing_shapes_keep_v2_and_never_echo_stock_context():
    trade = request([(C200.symbol, "buy"), (C205.symbol, "sell")], limit="1.10")
    assert "stock_context" not in trade_wire(trade)
    assert (
        "stock_context" not in evaluate(trade, policy(), evidence((C200, C205)), now=NOW)["trade"]
    )
    assert set(trade_wire(trade)) == {"legs", "quantity", "limit_price", "client_reference"}
    assert proposal_fingerprint(trade) == proposal_fingerprint(
        request([(C200.symbol, "buy"), (C205.symbol, "sell")], limit="1.10")
    )
    assert "standalone-proposal-v3" not in str(trade_wire(trade))


# ---------------------------------------------------------------- policy keys
def test_new_policy_keys_are_omitted_from_the_snapshot_at_defaults_and_shown_when_changed():
    default_meta = policy_metadata(AssessmentPolicy())
    for key in (
        "allowed_structures",
        "credit_min_credit_to_width",
        "credit_max_short_delta",
        "credit_dte_min",
        "credit_dte_max",
        "protective_max_premium_pct",
    ):
        assert key not in default_meta["policy_snapshot"]
    assert default_meta["supported_strategies"] == OLD_FOUR
    enabled = policy_metadata(policy())
    assert enabled["policy_snapshot"]["allowed_structures"] == ALL_EIGHT
    assert enabled["supported_strategies"] == ALL_EIGHT
    assert enabled["policy_version"] != default_meta["policy_version"]
    tuned = policy_metadata(AssessmentPolicy(credit_max_short_delta=Decimal("0.2")))
    assert tuned["policy_snapshot"]["credit_max_short_delta"] == "0.2"


def test_allowed_structures_validation_and_normalisation():
    assert AssessmentPolicy(
        allowed_structures=["protective_put", "long_call"]
    ).allowed_structures == ("long_call", "protective_put")
    assert AssessmentPolicy(allowed_structures=OLD_FOUR) == AssessmentPolicy()
    for bad in ([], ["iron_condor"], "long_call", [1]):
        with pytest.raises(ValidationError):
            AssessmentPolicy(allowed_structures=bad)
    with pytest.raises(ValidationError):
        AssessmentPolicy(credit_dte_min=60, credit_dte_max=30)
    # A stored payload without the new keys still loads at today's defaults.
    assert AssessmentPolicy.from_payload({"minimum_dte": 14}).allowed_structures == tuple(OLD_FOUR)
