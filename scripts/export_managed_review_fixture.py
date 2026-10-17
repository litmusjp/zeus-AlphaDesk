"""Export a portable contract fixture through AD's real evaluator/serializer."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from packages.connected.assessment_policy import AssessmentPolicy
from packages.connected.direct_evidence import TradeEvidence
from packages.connected.managed_review_gate import ManagedReviewGate, managed_assessment
from packages.connected.trade_assessment import TradeRequest, evaluate
from packages.domain.options import Greeks, OptionContract, OptionQuote
from packages.domain.workflow import CatalystFeatures


def main(output: Path, enabled: bool = False, revision: int = 3) -> None:
    now = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
    symbol = "AAPL261106C00200000"
    trade = TradeRequest.model_validate(
        {
            "legs": [{"symbol": symbol, "side": "buy"}],
            "quantity": 1,
            "limit_price": "1.84",
            "client_reference": "op2-managed-fixture",
        }
    )
    evidence = TradeEvidence(
        contracts=(
            OptionContract(
                contract_id="fixture-contract",
                symbol=symbol,
                underlying_symbol="AAPL",
                expiration=date(2026, 11, 6),
                strike=Decimal(200),
                option_type="call",
                multiplier=100,
                tradable=True,
                quote=OptionQuote(
                    bid="1.80",
                    ask="1.90",
                    bid_size=10,
                    ask_size=10,
                    quoted_at=now,
                    open_interest=1000,
                    greeks=Greeks(delta=".5", gamma=".01", theta="-.01", vega=".1"),
                ),
            ),
        ),
        features=CatalystFeatures(
            catalyst_confidence=".9",
            sentiment=".8",
            relative_volume=3,
            price_momentum=".8",
            gap_percent=1,
            market_confirmation=".8",
            sector_confirmation=".8",
            liquidity_score=".9",
        ),
        features_at=now,
        spot=Decimal(200),
        greeks_at=now,
        greeks_source="fixture-provider",
    )
    result = evaluate(trade, AssessmentPolicy(), evidence, now=now)
    if result["decision"] != "PASS":
        raise SystemExit(f"fixture proposal did not score PASS: {result['decision']}")
    gate = ManagedReviewGate(
        UUID("a1a1a1a1-a1a1-41a1-81a1-a1a1a1a1a1a1"),
        UUID("b2b2b2b2-b2b2-42b2-82b2-b2b2b2b2b2b2"),
        enabled,
        revision,
    )
    receipt = managed_assessment(result, gate, evaluated=not enabled)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(
        f"wrote AD serializer fixture decision={receipt['decision']} "
        f"revision={receipt['approval_gate']['revision']} bytes={output.stat().st_size}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--revision", type=int, default=3)
    args = parser.parse_args()
    main(args.output, args.enabled, args.revision)
