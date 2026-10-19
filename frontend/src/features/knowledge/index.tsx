import type { FrontendAddon } from "../types";
import { knowledgeFeatureEnabled } from "./capability";
import { KnowledgePage } from "./KnowledgePage";


export const knowledgeAddon: FrontendAddon = {
  id: "knowledge",
  label: "Knowledge",
  icon: (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      style={{ flexShrink: 0 }}
    >
      <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" />
      <line x1="5.5" y1="6" x2="10.5" y2="6" />
      <line x1="5.5" y1="9" x2="8.5" y2="9" />
    </svg>
  ),
  isEnabled: knowledgeFeatureEnabled,
  render: (props) => <KnowledgePage {...props} />,
};
