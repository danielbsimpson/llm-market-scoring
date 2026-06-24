// Minimal typed API client for the LLM Market Scoring backend.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface LlmHealth {
  ok: boolean;
  provider: string;
  base_url: string;
  default_model?: string;
  available_models?: string[];
  error?: string;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => getJson<{ status: string; env: string }>("/health"),
  llmHealth: () => getJson<LlmHealth>("/health/llm"),
};
