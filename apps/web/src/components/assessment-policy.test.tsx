import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AssessmentPolicyPage } from "./assessment-policy";

vi.mock("@/lib/api", () => ({
  deskFetch: vi.fn(() => Promise.resolve({})),
}));

describe("AssessmentPolicyPage", () => {
  it("shows original defaults in every editable range and preserves direction guidance", () => {
    render(<AssessmentPolicyPage />);

    for (const range of [
      "[0 ~ 100, 65]",
      "[0 ~ 100, 12]%",
      "[0 ~ 1, 0.60]",
      "[1 ~ 365, 14] days",
      "[1 ~ 365, 45] days",
      "[0 ~ 2, 1.00]",
      "blank (default: blank/null), or [0 ~ 1,000,000] contracts",
      "[0 ~ 604,800, 86400] seconds",
      "[0 ~ 0.50, 0.20]",
      "[1 ~ 1,000,000, 25] contracts",
      "[0 ~ 300, 120] seconds",
      "(0 ~ 100,000, 1] contracts",
      "[0 ~ 0.30, 0.30]",
      "(0 ~ 1,000, 250] dollars",
      "[1 ~ 10, 10] contracts",
      "(0 ~ 5, 0.50]% of equity",
      "(0 ~ 10, 4.0]% of equity",
      "(0 ~ 25, 10.0]% of equity",
      "(0 ~ 5, 2.0]% of equity",
      "(0 ~ 20, 10.0]%",
      "[1 ~ 100, 8] structures",
      "(0 ~ 1,000,000, 5000] delta units",
      "(0 ~ 1,000,000, 1000] gamma units",
      "(0 ~ 1,000,000, 1000] theta units",
      "(0 ~ 1,000,000, 5000] vega units",
    ]) {
      expect(screen.getAllByText(range).length).toBeGreaterThan(0);
    }

    expect(screen.getByText("Fixed: required; there is no editable range.")).toBeInTheDocument();
    expect(screen.getByText(/Lower allows less interest; higher requires more interest\. Blank applies no minimum\./)).toBeInTheDocument();
    expect(screen.getByText(/Lower allows less vega exposure; higher allows more vega exposure\./)).toBeInTheDocument();
  });
});
