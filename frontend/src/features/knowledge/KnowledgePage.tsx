import { useEffect, useMemo, useRef, useState } from "react";

import {
  addFileKnowledgeSource,
  addTextKnowledgeSource,
  deleteKnowledgeSource,
  deleteProviderKey,
  getEmbeddingProfile,
  getEmbeddingProviders,
  getKnowledgeSources,
  searchKnowledge,
  setProviderKey,
  testEmbeddingProfile,
  updateEmbeddingProfile,
  type EmbeddingProfile,
  type KnowledgeSearchResult,
  type KnowledgeSource,
} from "./api";
import {
  modelsForProvider,
  selectedModelIsAvailable,
  type EmbeddingProvider,
} from "./logic";

type Props = {
  token: string;
  pushToast: (message: string) => void;
  onAuthenticationExpired: () => void;
};

export function KnowledgePage({ token, pushToast, onAuthenticationExpired }: Props) {
  const [providers, setProviders] = useState<EmbeddingProvider[]>([]);
  const [profile, setProfile] = useState<EmbeddingProfile | null>(null);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [results, setResults] = useState<KnowledgeSearchResult[]>([]);
  const [providerId, setProviderId] = useState("");
  const [modelId, setModelId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<
    "test" | "save" | "paste" | "file" | "search" | "key" | null
  >(null);
  const [error, setError] = useState("");
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const fileInput = useRef<HTMLInputElement>(null);

  function handleError(reason: unknown) {
    const message = String(reason);
    if (message.toLowerCase().includes("authentication")) onAuthenticationExpired();
    else setError(message);
  }

  async function loadKnowledge() {
    const [loadedProviders, loadedProfile, loadedSources] = await Promise.all([
      getEmbeddingProviders(token),
      getEmbeddingProfile(token),
      getKnowledgeSources(token),
    ]);
    setProviders(loadedProviders);
    setProfile(loadedProfile);
    setSources(loadedSources);
    setProviderId(loadedProfile.provider_id);
    setModelId(loadedProfile.model_id);
  }

  useEffect(() => {
    let active = true;
    loadKnowledge()
      .catch((reason: unknown) => {
        if (active) handleError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // Authentication expiration is a stable callback owned by App.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, onAuthenticationExpired]);

  useEffect(() => {
    if (profile?.status !== "reindexing") return;
    const timer = window.setInterval(() => {
      getEmbeddingProfile(token)
        .then((nextProfile) => setProfile(nextProfile))
        .catch(handleError);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [profile?.status, token]);

  const models = useMemo(
    () => modelsForProvider(providers, providerId),
    [providers, providerId],
  );
  const canSubmit = selectedModelIsAvailable(providers, providerId, modelId) && busy === null;
  const canAddSources = profile?.active === true && profile.status === "ready" && busy === null;

  async function saveKey(targetProviderId: string) {
    const draft = (keyDrafts[targetProviderId] ?? "").trim();
    if (!draft) return;
    setBusy("key");
    setError("");
    try {
      const updated = await setProviderKey(token, targetProviderId, draft);
      setProviders(updated);
      setKeyDrafts((current) => ({ ...current, [targetProviderId]: "" }));
      pushToast("API key saved — run Test configuration to verify it");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  async function removeKey(targetProviderId: string) {
    setBusy("key");
    setError("");
    try {
      setProviders(await deleteProviderKey(token, targetProviderId));
      pushToast("API key removed");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  async function testConnection() {
    setBusy("test");
    setError("");
    try {
      const result = await testEmbeddingProfile(token, providerId, modelId);
      pushToast(`Embedding provider ready · ${result.dimensions} dimensions`);
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  async function saveProfile() {
    setBusy("save");
    setError("");
    try {
      const saved = await updateEmbeddingProfile(token, providerId, modelId);
      setProfile(saved);
      pushToast(saved.status === "reindexing" ? "Reindexing started" : "Embedding model saved");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  async function addPaste() {
    if (!sourceName.trim() || !sourceText.trim()) return;
    setBusy("paste");
    setError("");
    try {
      const source = await addTextKnowledgeSource(token, sourceName.trim(), sourceText.trim());
      setSources((current) => [source, ...current]);
      setSourceName("");
      setSourceText("");
      pushToast("Source indexed");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  async function addFile(file: File) {
    setBusy("file");
    setError("");
    try {
      const source = await addFileKnowledgeSource(token, file);
      setSources((current) => [source, ...current]);
      pushToast("File indexed");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function removeSource(sourceId: string) {
    setError("");
    try {
      await deleteKnowledgeSource(token, sourceId);
      setSources((current) => current.filter((source) => source.id !== sourceId));
      setResults((current) => current.filter((result) => result.source_id !== sourceId));
      pushToast("Source deleted");
    } catch (reason) {
      handleError(reason);
    }
  }

  async function runSearch(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy("search");
    setError("");
    try {
      setResults(await searchKnowledge(token, query.trim()));
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="content-page knowledge-page" style={{ padding: "var(--page-gutter)", maxWidth: 1000, margin: "0 auto" }}>
      <h1 style={{ margin: 0, fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em" }}>
        Knowledge
      </h1>
      <div style={{ fontSize: 13, color: "#6B6B66", marginTop: 6 }}>
        Ground voice answers in sources you control.
      </div>

      <section style={cardStyle}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>Provider API keys</div>
        <div style={{ color: "#6B6B66", fontSize: 12.5, marginTop: 5 }}>
          Bring your own key for OpenAI or Google Gemini. Keys are stored on the
          Talkstream server for your account only and are never shown again in full.
        </div>
        {loading ? (
          <div style={{ color: "#A6A6A0", fontSize: 13, marginTop: 20 }}>Loading providers…</div>
        ) : (
          providers.map((provider) => (
            <div key={provider.id} style={keyRowStyle}>
              <div style={{ minWidth: 130 }}>
                <div style={{ fontSize: 13.5, fontWeight: 500 }}>{provider.label}</div>
                <div
                  style={{
                    fontSize: 11.5,
                    marginTop: 3,
                    color: provider.available ? "#1F7A46" : "#9A6A0B",
                  }}
                >
                  {provider.key_set
                    ? `Your key · ${provider.key_hint ?? ""}`
                    : provider.available
                      ? "Server key active"
                      : "No key — add one to enable"}
                </div>
              </div>
              <input
                type="password"
                value={keyDrafts[provider.id] ?? ""}
                onChange={(event) =>
                  setKeyDrafts((current) => ({
                    ...current,
                    [provider.id]: event.target.value,
                  }))
                }
                placeholder={
                  provider.key_set ? "Replace API key…" : `${provider.label} API key…`
                }
                autoComplete="off"
                style={{ ...inputStyle, flex: 1 }}
              />
              <button
                disabled={busy !== null || !(keyDrafts[provider.id] ?? "").trim()}
                onClick={() => void saveKey(provider.id)}
                style={primaryButton}
              >
                {busy === "key" ? "Saving…" : "Save key"}
              </button>
              {provider.key_set && (
                <button
                  disabled={busy !== null}
                  onClick={() => void removeKey(provider.id)}
                  style={dangerButton}
                >
                  Remove
                </button>
              )}
            </div>
          ))
        )}
      </section>

      <section style={cardStyle}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>Embedding model</div>
            <div style={{ color: "#6B6B66", fontSize: 12.5, marginTop: 5 }}>
              Changing the model safely reindexes existing sources.
            </div>
          </div>
          {profile && <StatusPill status={profile.status} />}
        </div>

        {loading ? (
          <div style={{ color: "#A6A6A0", fontSize: 13, marginTop: 20 }}>Loading settings…</div>
        ) : (
          <>
            <div style={twoColumnStyle}>
              <label style={labelStyle}>
                Provider
                <select
                  value={providerId}
                  onChange={(event) => {
                    const next = event.target.value;
                    setProviderId(next);
                    const nextModels = modelsForProvider(providers, next);
                    setModelId(nextModels.find((model) => model.default)?.id ?? nextModels[0]?.id ?? "");
                  }}
                  style={inputStyle}
                >
                  {providers.map((provider) => (
                    <option key={provider.id} value={provider.id} disabled={!provider.available}>
                      {provider.label}{provider.available ? "" : " — unavailable"}
                    </option>
                  ))}
                </select>
              </label>
              <label style={labelStyle}>
                Model
                <select value={modelId} onChange={(event) => setModelId(event.target.value)} style={inputStyle}>
                  {models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.label} · {model.dimensions}d
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div style={noticeStyle}>
              Privacy: source text and search queries are sent to the selected remote embedding
              provider. Credentials stay on the Talkstream server.
            </div>
            {profile?.status === "reindexing" && (
              <div aria-live="polite" style={progressStyle}>
                Reindexing {profile.reindex_processed_chunks ?? 0} of{
                  " "}{profile.reindex_total_chunks ?? "…"} chunks. Existing knowledge remains active.
              </div>
            )}
            {profile?.status === "failed" && (
              <div role="alert" style={failedStyle}>
                Reindexing failed. The previous model remains active; save the model again to retry.
              </div>
            )}
            <div style={buttonRowStyle}>
              <button disabled={!canSubmit} onClick={testConnection} style={secondaryButton}>
                {busy === "test" ? "Testing…" : "Test configuration"}
              </button>
              <button disabled={!canSubmit} onClick={saveProfile} style={primaryButton}>
                {busy === "save" ? "Saving…" : profile?.status === "failed" ? "Retry model change" : "Save model"}
              </button>
            </div>
          </>
        )}
      </section>

      <section style={cardStyle}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>Add source</div>
        <div style={twoColumnStyle}>
          <div style={{ display: "grid", gap: 10 }}>
            <input value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder="Source name" style={inputStyle} />
            <textarea value={sourceText} onChange={(event) => setSourceText(event.target.value)} placeholder="Paste trusted text here…" rows={6} style={{ ...inputStyle, resize: "vertical" }} />
            <button disabled={!canAddSources || !sourceName.trim() || !sourceText.trim()} onClick={addPaste} style={primaryButton}>
              {busy === "paste" ? "Indexing…" : "Index pasted text"}
            </button>
          </div>
          <div style={{ ...noticeStyle, marginTop: 0, display: "grid", placeItems: "center", textAlign: "center" }}>
            <div>
              <div style={{ color: "#111110", fontWeight: 600 }}>PDF, TXT, or Markdown</div>
              <div style={{ margin: "6px 0 14px" }}>Text-based files up to the configured server limit.</div>
              <input ref={fileInput} type="file" accept=".pdf,.txt,.md" disabled={!canAddSources} onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void addFile(file);
              }} />
            </div>
          </div>
        </div>
      </section>

      <section style={cardStyle}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 12 }}>Sources</div>
        {sources.length === 0 ? (
          <div style={{ color: "#A6A6A0", fontSize: 13 }}>No sources indexed yet.</div>
        ) : sources.map((source) => (
          <div key={source.id} className="knowledge-source-row" style={sourceRowStyle}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis" }}>{source.name}</div>
              <div style={{ color: "#A6A6A0", fontSize: 11.5, marginTop: 4 }}>
                {source.chunk_count} chunks · {source.character_count.toLocaleString()} characters
              </div>
            </div>
            <StatusPill status={source.status} />
            <button onClick={() => void removeSource(source.id)} style={dangerButton}>Delete</button>
          </div>
        ))}
      </section>

      <section style={cardStyle}>
        <form onSubmit={runSearch} style={{ display: "flex", gap: 10 }}>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Test a knowledge-base question…" style={{ ...inputStyle, flex: 1 }} />
          <button disabled={!profile?.active || !query.trim() || busy !== null} style={primaryButton}>
            {busy === "search" ? "Searching…" : "Search"}
          </button>
        </form>
        <div aria-live="polite" style={{ marginTop: results.length ? 14 : 0 }}>
          {results.map((result) => (
            <article key={result.chunk_id} style={resultStyle}>
              <div style={{ color: "#111110", fontSize: 13, lineHeight: 1.6 }}>{result.excerpt}</div>
              <div style={{ color: "#4A6CF7", fontSize: 11.5, marginTop: 7 }}>
                According to {result.source_name}{result.page_number ? `, page ${result.page_number}` : ""}
              </div>
            </article>
          ))}
          {query && busy === null && results.length === 0 && (
            <div style={{ color: "#A6A6A0", fontSize: 12.5 }}>No supporting result yet.</div>
          )}
        </div>
      </section>

      {error && <div role="alert" style={{ color: "#B3352E", fontSize: 12.5, marginTop: 12 }}>{error}</div>}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const ready = status === "ready" || status === "indexed";
  return <span style={{ alignSelf: "center", borderRadius: 999, padding: "5px 10px", background: ready ? "#EAF7EF" : "#FCF1E5", color: ready ? "#1F7A46" : "#9A6A0B", fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap" }}>{status.replaceAll("_", " ")}</span>;
}

const cardStyle = { background: "#FFFFFF", border: "1px solid #E5E5E1", borderRadius: 14, marginTop: 16, padding: 22 };
const twoColumnStyle = { display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 12, marginTop: 18 };
const labelStyle = { display: "grid", gap: 6, color: "#6B6B66", fontSize: 12 };
const inputStyle = { width: "100%", border: "1px solid #D5D5D0", borderRadius: 9, padding: "10px 12px", background: "#FFFFFF", color: "#111110", fontSize: 13, boxSizing: "border-box" as const };
const noticeStyle = { background: "#F7F7F5", borderRadius: 10, color: "#6B6B66", fontSize: 12, lineHeight: 1.5, marginTop: 14, padding: "11px 13px" };
const progressStyle = { background: "#EEF3FF", borderRadius: 10, color: "#3659B8", fontSize: 12, lineHeight: 1.5, marginTop: 10, padding: "11px 13px" };
const failedStyle = { background: "#FFF1F0", borderRadius: 10, color: "#9E302B", fontSize: 12, lineHeight: 1.5, marginTop: 10, padding: "11px 13px" };
const buttonRowStyle = { display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 16 };
const secondaryButton = { border: "1px solid #D5D5D0", borderRadius: 999, background: "#FFFFFF", color: "#111110", fontSize: 13, padding: "8px 16px" };
const primaryButton = { border: "none", borderRadius: 999, background: "#111110", color: "#FFFFFF", fontSize: 13, padding: "9px 18px" };
const dangerButton = { border: "none", background: "transparent", color: "#B3352E", fontSize: 12, padding: "6px 8px" };
const sourceRowStyle = { display: "grid", gridTemplateColumns: "minmax(0,1fr) auto auto", alignItems: "center", gap: 12, borderTop: "1px solid #F0F0EC", padding: "13px 0" };
const keyRowStyle = { display: "flex", alignItems: "center", gap: 12, borderTop: "1px solid #F0F0EC", padding: "13px 0", marginTop: 12, flexWrap: "wrap" as const };
const resultStyle = { borderTop: "1px solid #F0F0EC", padding: "12px 2px" };
