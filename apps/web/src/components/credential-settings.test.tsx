import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CredentialSettings } from "./credential-settings";

const deskFetch = vi.fn();
vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

describe("CredentialSettings request lifecycle", () => {
  beforeEach(() => deskFetch.mockReset());

  it("reports a credential save as successful when its status refresh fails", async () => {
    deskFetch
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error("status service unavailable"));

    render(<CredentialSettings />);
    await waitFor(() => expect(deskFetch).toHaveBeenCalledWith("/desk/credentials"));

    fireEvent.change(screen.getAllByLabelText("API key")[0], { target: { value: "test-api-key-123" } });
    fireEvent.change(screen.getByLabelText("OpenRouter model"), { target: { value: "openai/gpt-4.1-mini" } });
    const openRouterForm = screen.getByRole("heading", { name: "OpenRouter" }).closest("form");
    fireEvent.click(within(openRouterForm as HTMLElement).getByRole("button", { name: "Test & save encrypted" }));

    await waitFor(() => expect(screen.getByText(/saved, but status could not be refreshed/i)).toBeInTheDocument());
    expect(screen.getByText(/status service unavailable/i)).toBeInTheDocument();
    expect(deskFetch).toHaveBeenNthCalledWith(2, "/desk/credentials/openrouter", expect.objectContaining({ method: "PUT" }));
  });

  it("reports credential removal as successful when its status refresh fails", async () => {
    deskFetch
      .mockResolvedValueOnce([{ provider: "OPENROUTER", configured: true, enabled: true, validation_status: "VERIFIED", fingerprint: "abcd", configuration: { model: "openai/gpt-4.1-mini", active: true }, validated_at: null }])
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("status service unavailable"));

    render(<CredentialSettings />);
    const removeButton = await screen.findByRole("button", { name: "Delete OpenRouter credentials" });
    fireEvent.click(removeButton);

    await waitFor(() => expect(screen.getByText(/removed, but status could not be refreshed/i)).toBeInTheDocument());
    expect(screen.getByText(/status service unavailable/i)).toBeInTheDocument();
    expect(deskFetch).toHaveBeenNthCalledWith(2, "/desk/credentials/openrouter", expect.objectContaining({ method: "DELETE" }));
  });
});
