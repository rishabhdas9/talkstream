import { resolveApiBaseUrl } from "./endpoints";

export type PublicCapabilities = {
  dev_login?: boolean;
  features?: Record<string, boolean | undefined>;
  speech?: {
    transport: string;
    model: string;
    configured_language: string;
    language_detection: boolean;
    experimental: boolean;
  };
};

const API_BASE_URL = resolveApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  window.location.origin,
);

export async function getCapabilities(): Promise<PublicCapabilities> {
  const response = await fetch(`${API_BASE_URL}/capabilities`);
  if (!response.ok) return {};
  return (await response.json()) as PublicCapabilities;
}
