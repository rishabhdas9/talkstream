export function resolveApiBaseUrl(
  configuredUrl: string | undefined,
  pageOrigin: string,
): string {
  const baseUrl = configuredUrl?.trim() || pageOrigin;
  return baseUrl.replace(/\/+$/, "");
}

export function voiceWebSocketUrl(baseUrl: string, ticket: string): string {
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/voice";
  url.search = new URLSearchParams({ ticket }).toString();
  url.hash = "";
  return url.toString();
}
