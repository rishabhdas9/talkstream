import type { PublicCapabilities } from "../../api/capabilities";

export function knowledgeFeatureEnabled(
  capabilities: PublicCapabilities | undefined,
): boolean {
  return capabilities?.features?.knowledge_rag === true;
}
