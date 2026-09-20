import { AgentApiPage } from "@/components/agent-api";
import { WorkspaceShell } from "@/components/workspace-shell";

export default function AgentApiRoute() {
  return <WorkspaceShell title="Agent API & MCP" description="Let an external agent request a read-only Candidate Assessment."><AgentApiPage /></WorkspaceShell>;
}
