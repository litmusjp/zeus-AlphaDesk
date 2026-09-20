export type PositionRecommendationLabel = "HOLD" | "BUY MORE" | "SELL";

export type PositionRecommendation = {
  label: PositionRecommendationLabel;
  rationale: string;
};

type PositionForRecommendation = {
  unrealized_pl: string | number | null | undefined;
  current_price?: string | number | null;
};

function isFiniteNumber(value: string | number | null | undefined): boolean {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

/**
 * Keep position guidance conservative until a fresh Candidate Assessment is
 * carried alongside the broker projection. P/L and price are not assessment
 * evidence and must not create a buy or sell signal.
 */
export function recommendPositionAction(
  position: PositionForRecommendation,
): PositionRecommendation {
  if (!isFiniteNumber(position.unrealized_pl) || !isFiniteNumber(position.current_price)) {
    return {
      label: "HOLD",
      rationale: "Position data is incomplete or invalid, so no change is recommended.",
    };
  }

  return {
    label: "HOLD",
    rationale:
      "Fresh Candidate Assessment evidence is unavailable. Do not BUY MORE without a new assessment, human approval, Guardian, paper-only and execution checks.",
  };
}
