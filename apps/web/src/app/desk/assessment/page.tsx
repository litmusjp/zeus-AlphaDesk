import { AssessmentPolicyPage } from "@/components/assessment-policy";
import { WorkspaceShell } from "@/components/workspace-shell";

export default function AssessmentPage() {
  return <WorkspaceShell title="Candidate Assessment" description="Understand and tune how paper option candidates are found, sized, and checked."><AssessmentPolicyPage /></WorkspaceShell>;
}
