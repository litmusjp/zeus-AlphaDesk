import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AdminConsole } from "./admin-console";

const deskFetch = vi.fn();

vi.mock("@/lib/api", () => ({ deskFetch: (...args: unknown[]) => deskFetch(...args) }));

describe("AdminConsole", () => {
  beforeEach(() => deskFetch.mockReset());

  function mockAdminResponses(identity: Record<string, unknown>) {
    deskFetch.mockImplementation((path: string) => {
      if (path === "/identity/me") return Promise.resolve(identity);
      if (path === "/desk/credentials") return Promise.resolve([]);
      if (path === "/desk/assessment-policy") return Promise.resolve({});
      if (path === "/desk/agent-api-keys") return Promise.resolve([]);
      if (path === "/admin/workspace") return Promise.resolve({ workspace_id: "workspace-1", status: "ONBOARDING", created: true, watchlist_count: 7 });
      return Promise.resolve({});
    });
  }

  it("provisions the existing admin identity without an invitation", async () => {
    mockAdminResponses({ email: "admin@example.test", is_admin: true, workspace_id: null, workspace_status: null });

    render(<AdminConsole />);
    const button = await screen.findByRole("button", { name: "Create my Paper Workspace" });
    fireEvent.click(button);

    await waitFor(() => expect(deskFetch).toHaveBeenCalledWith(
      "/admin/workspace",
      { method: "POST" },
    ));
    expect(await screen.findByText(/Your Connected Paper Workspace is ready for provider setup/i)).toBeInTheDocument();
    expect(screen.queryByText("CONNECTED PAPER WORKSPACE")).not.toBeInTheDocument();
  });

  it("keeps candidate assessment on its dedicated page", async () => {
    mockAdminResponses({
      email: "admin@example.test",
      is_admin: true,
      workspace_id: "workspace-1",
      workspace_status: "ACTIVE",
    });

    render(<AdminConsole />);

    expect(await screen.findByRole("heading", { name: "Credential Settings" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Candidate Assessment" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent API & MCP" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What the assessment returns" })).toBeInTheDocument();
    expect(screen.getByText(/return the Market Scanner signal score when the request includes/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Changing the assessment profile" })).toBeInTheDocument();
    expect(screen.getByText(/cannot change the profile/i)).toBeInTheDocument();
    expect(screen.getByText("ADMINISTRATOR ACCESS")).toBeInTheDocument();
    expect(screen.queryByText("Ready for provider setup")).not.toBeInTheDocument();
    expect(screen.queryByText("CONNECTED PAPER WORKSPACE")).not.toBeInTheDocument();
  });
});
