// Pure selection helpers for the optional Knowledge add-on.
export type EmbeddingModel = {
  id: string;
  label: string;
  dimensions: number;
  default: boolean;
};

export type EmbeddingProvider = {
  id: string;
  label: string;
  available: boolean;
  key_set: boolean;
  key_hint: string | null;
  models: EmbeddingModel[];
};

export function modelsForProvider(
  providers: EmbeddingProvider[],
  providerId: string,
): EmbeddingModel[] {
  return providers.find((provider) => provider.id === providerId)?.models ?? [];
}

export function selectedModelIsAvailable(
  providers: EmbeddingProvider[],
  providerId: string,
  modelId: string,
): boolean {
  const provider = providers.find((candidate) => candidate.id === providerId);
  return provider?.available === true && provider.models.some((model) => model.id === modelId);
}
