from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from packages.connected.assessment_policy import AssessmentPolicy
from packages.domain.options import EligibilityResult, LiquidityPolicy, OptionContract, OptionType
from packages.options.liquidity import evaluate_contract


class ScanMode(StrEnum):
    PRE_SCAN = "PRE_SCAN"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True)
class OptionScanDiagnostics:
    total_contracts: int
    requested_type_contracts: int
    strict_eligible_contracts: int
    selected_contracts: int
    rejection_counts: dict[str, int]


@dataclass(frozen=True)
class OptionScanSelection:
    selected: tuple[OptionContract, ...]
    diagnostics: OptionScanDiagnostics


def _policy(*, pre_scan: bool, underlying_symbol: str, policy: AssessmentPolicy) -> LiquidityPolicy:
    return LiquidityPolicy(
        supported_underlyings=frozenset({underlying_symbol}),
        min_dte=policy.minimum_dte,
        max_dte=policy.maximum_dte,
        max_spread_ratio=policy.pre_scan_max_spread_ratio
        if pre_scan
        else policy.execution_max_spread_ratio,
        min_open_interest=policy.pre_scan_min_open_interest
        if pre_scan
        else policy.execution_min_open_interest,
        max_quote_age_seconds=policy.pre_scan_max_quote_age_seconds
        if pre_scan
        else policy.execution_max_quote_age_seconds,
        min_quote_size=policy.minimum_quote_size,
        max_strike_distance_ratio=policy.maximum_strike_distance_ratio,
        require_greeks=policy.require_greeks,
    )


def select_contracts(
    contracts: tuple[OptionContract, ...],
    *,
    underlying_price: Decimal,
    wanted_type: OptionType,
    as_of: datetime,
    mode: ScanMode,
    policy: AssessmentPolicy | None = None,
) -> OptionScanSelection:
    policy = policy or AssessmentPolicy()
    underlying_symbol = contracts[0].underlying_symbol if contracts else ""
    strict_policy = _policy(pre_scan=False, underlying_symbol=underlying_symbol, policy=policy)
    selection_policy = _policy(
        pre_scan=mode is ScanMode.PRE_SCAN,
        underlying_symbol=underlying_symbol,
        policy=policy,
    )
    rejection_counts: Counter[str] = Counter()
    requested_type_contracts = 0
    strict_eligible_contracts = 0
    selected: list[OptionContract] = []

    for contract in contracts:
        if contract.option_type is not wanted_type:
            continue
        requested_type_contracts += 1
        strict_result: EligibilityResult = evaluate_contract(
            contract,
            underlying_price=underlying_price,
            policy=strict_policy,
            as_of=as_of,
        )
        if strict_result.eligible:
            strict_eligible_contracts += 1
        else:
            rejection_counts.update(strict_result.reasons)
        selection_result = evaluate_contract(
            contract,
            underlying_price=underlying_price,
            policy=selection_policy,
            as_of=as_of,
        )
        if selection_result.eligible:
            selected.append(contract)

    diagnostics = OptionScanDiagnostics(
        total_contracts=len(contracts),
        requested_type_contracts=requested_type_contracts,
        strict_eligible_contracts=strict_eligible_contracts,
        selected_contracts=len(selected),
        rejection_counts=dict(sorted(rejection_counts.items())),
    )
    return OptionScanSelection(selected=tuple(selected), diagnostics=diagnostics)
