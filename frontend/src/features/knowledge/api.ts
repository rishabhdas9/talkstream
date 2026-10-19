import { resolveApiBaseUrl } from "../../api/endpoints";
import { errorForResponse } from "../../api/errors";
import type { EmbeddingProvider } from "./logic";

const API_BASE_URL = resolveApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  window.location.origin,
);

export type EmbeddingProfile = {
  configured: boolean;
  provider_id: string;
  model_id: string;
  dimensions: number;
  status: "ready" | "reindexing" | "failed" | "provider_unavailable";
  active: boolean;
  generation_id: string | null;
  reindex_processed_chunks: number | null;
  reindex_total_chunks: number | null;
};

export type KnowledgeSource = {
  id: string;
  name: string;
  media_type: string;
  status: "processing" | "indexed" | "failed";
  character_count: number;
  chunk_count: number;
  error_message: string | null;
  created_at: string;
};

export type KnowledgeSearchResult = {
  chunk_id: string;
  source_id: string;
  source_name: string;
  excerpt: string;
  page_number: number | null;
  score: number;
};

async function knowledgeJson<T>(
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
  if (!response.ok) throw await errorForResponse(response);
  return (await response.json()) as T;
}

export async function getEmbeddingProviders(token: string): Promise<EmbeddingProvider[]> {
  const response = await knowledgeJson<{ providers: EmbeddingProvider[] }>(
    "/knowledge/providers",
    token,
  );
  return response.providers;
}

export async function setProviderKey(
  token: string,
  providerId: string,
  apiKey: string,
): Promise<EmbeddingProvider[]> {
  const response = await knowledgeJson<{ providers: EmbeddingProvider[] }>(
    `/knowledge/providers/${encodeURIComponent(providerId)}/key`,
    token,
    { method: "PUT", body: JSON.stringify({ api_key: apiKey }) },
  );
  return response.providers;
}

export async function deleteProviderKey(
  token: string,
  providerId: string,
): Promise<EmbeddingProvider[]> {
  const response = await knowledgeJson<{ providers: EmbeddingProvider[] }>(
    `/knowledge/providers/${encodeURIComponent(providerId)}/key`,
    token,
    { method: "DELETE" },
  );
  return response.providers;
}

export function getEmbeddingProfile(token: string): Promise<EmbeddingProfile> {
  return knowledgeJson("/knowledge/profile", token);
}

export function testEmbeddingProfile(
  token: string,
  providerId: string,
  modelId: string,
): Promise<{ ok: boolean; dimensions: number }> {
  return knowledgeJson("/knowledge/profile/test", token, {
    method: "POST",
    body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
  });
}

export function updateEmbeddingProfile(
  token: string,
  providerId: string,
  modelId: string,
): Promise<EmbeddingProfile> {
  return knowledgeJson("/knowledge/profile", token, {
    method: "PUT",
    body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
  });
}

export async function getKnowledgeSources(token: string): Promise<KnowledgeSource[]> {
  const response = await knowledgeJson<{ sources: KnowledgeSource[] }>(
    "/knowledge/sources",
    token,
  );
  return response.sources;
}

export function addTextKnowledgeSource(
  token: string,
  name: string,
  text: string,
): Promise<KnowledgeSource> {
  return knowledgeJson("/knowledge/sources/text", token, {
    method: "POST",
    body: JSON.stringify({ name, text }),
  });
}

export async function addFileKnowledgeSource(
  token: string,
  file: File,
): Promise<KnowledgeSource> {
  const body = new FormData();
  body.set("file", file);
  const response = await fetch(`${API_BASE_URL}/knowledge/sources/file`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body,
  });
  if (!response.ok) throw await errorForResponse(response);
  return (await response.json()) as KnowledgeSource;
}

export async function deleteKnowledgeSource(token: string, sourceId: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/knowledge/sources/${encodeURIComponent(sourceId)}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw await errorForResponse(response);
}

export async function searchKnowledge(
  token: string,
  query: string,
): Promise<KnowledgeSearchResult[]> {
  const response = await knowledgeJson<{ results: KnowledgeSearchResult[] }>(
    "/knowledge/search",
    token,
    { method: "POST", body: JSON.stringify({ query, limit: 5 }) },
  );
  return response.results;
}
