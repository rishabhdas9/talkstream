export const AUTHENTICATION_EXPIRED_MESSAGE =
  "Your session has expired. Please sign in again.";

export class AuthenticationExpiredError extends Error {
  constructor() {
    super(AUTHENTICATION_EXPIRED_MESSAGE);
    this.name = "AuthenticationExpiredError";
  }
}

export async function errorForResponse(response: Response): Promise<Error> {
  if (response.status === 401) return new AuthenticationExpiredError();
  const body = (await response.json().catch(() => null)) as { detail?: string } | null;
  return new Error(body?.detail ?? `Request failed with status ${response.status}`);
}

export function isAuthenticationExpiredError(
  error: unknown,
): error is AuthenticationExpiredError {
  return error instanceof AuthenticationExpiredError;
}

export function errorForVoiceClose(code: number): Error {
  return code === 4401
    ? new AuthenticationExpiredError()
    : new Error("Voice WebSocket connection failed");
}
