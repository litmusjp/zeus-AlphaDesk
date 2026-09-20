import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceShell } from "./workspace-shell";

const deskFetch = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin",
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));
vi.mock("@/lib/api", () => ({
  deskFetch: (...args: unknown[]) => deskFetch(...args),
  supabaseBrowser: () => null,
}));

describe("WorkspaceShell admin navigation", () => {
  beforeEach(() => deskFetch.mockReset());

  it("shows admin navigation and disables operator tools before provisioning", async () => {
    deskFetch.mockResolvedValue({
      email: "admin@example.test",
      is_admin: true,
      workspace_id: null,
      workspace_status: null,
    });
    render(<WorkspaceShell title="Admin" description="Console"><div>Body</div></WorkspaceShell>);

    expect(await screen.findByRole("link", { name: /Admin Console/ })).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "connected paper workspace navigation" });
    const links = Array.from(navigation.querySelectorAll("a")).map((link) => link.textContent?.trim());
    expect(links).toEqual(["Admin Console", "Access & Invitations", "Candidate Assessment", "Audit & Guardian"]);
    expect(screen.queryByRole("link", { name: /Credential Settings/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Agent API & MCP/ })).not.toBeInTheDocument();
    expect(screen.getByText("Workspace Dashboard").closest("span")).toHaveAttribute("aria-disabled", "true");
  });
});
