import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InvitationAdmin } from "./invitation-admin";

vi.mock("@/lib/api", () => ({ deskFetch: vi.fn(() => Promise.resolve([])) }));

describe("InvitationAdmin", () => {
  it("contains no hackathon copy", () => {
    render(<InvitationAdmin />);
    expect(document.body.textContent).not.toMatch(/hackathon/i);
    expect(screen.getByPlaceholderText("Partner access")).toBeInTheDocument();
  });
});
