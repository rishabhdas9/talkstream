import { resolveApiBaseUrl, voiceWebSocketUrl as buildVoiceUrl } from "./endpoints";
import { errorForResponse } from "./errors";

const API_BASE_URL = resolveApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  window.location.origin,
);

type TokenResponse = {
  access_token: string;
};

export type WalletTransaction = {
  id: string;
  amount_paise: number;
  kind: "topup" | "usage";
  usage_session_id: string | null;
  created_at: string;
};

export type WalletResponse = {
  balance_paise: number;
  price_per_minute_paise: number;
  daily_allowance_paise: number;
  max_recharge_paise: number;
  // null means top-ups are not limited per day.
  topups_left_today: number | null;
  recent_transactions: WalletTransaction[];
};

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await errorForResponse(response);
  }
  return (await response.json()) as T;
}

export async function requestOtp(email: string): Promise<void> {
  await postJson("/auth/request-otp", { email });
}

export async function verifyOtp(email: string, code: string): Promise<string> {
  const response = await postJson<TokenResponse>("/auth/verify-otp", { email, code });
  return response.access_token;
}

export async function devLogin(): Promise<string> {
  const response = await postJson<TokenResponse>("/auth/dev-login", {});
  return response.access_token;
}

export function voiceWebSocketUrl(ticket: string): string {
  return buildVoiceUrl(API_BASE_URL, ticket);
}

export async function requestVoiceTicket(token: string): Promise<string> {
  const response = await authorizedJson<{ ticket: string }>("/auth/voice-ticket", token, { method: "POST" });
  return response.ticket;
}

export async function getCurrentUser(token: string): Promise<{ id: string; isAdmin: boolean }> {
  const user = await authorizedJson<{ id: unknown; is_admin?: unknown }>("/auth/me", token);
  if (typeof user.id !== "string" || !user.id.trim() || [...user.id].length > 128 || /\p{Surrogate}/u.test(user.id)) {
    throw new Error("Invalid authenticated user identity");
  }
  return { id: user.id, isAdmin: user.is_admin === true };
}

async function authorizedJson<T>(
  path: string,
  token: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...options.headers,
    },
  });
  if (!response.ok) {
    throw await errorForResponse(response);
  }
  return (await response.json()) as T;
}

export async function getWallet(token: string): Promise<WalletResponse> {
  return authorizedJson<WalletResponse>("/wallet", token);
}

export async function rechargeWallet(
  token: string,
  amountPaise: number,
): Promise<{ balance_paise: number; transaction: WalletTransaction }> {
  return authorizedJson("/wallet/recharge", token, {
    method: "POST",
    body: JSON.stringify({ amount_paise: amountPaise }),
  });
}
