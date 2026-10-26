/**
 * Calls the Booth Agent backend.
 * In demo mode (no Firebase), requests go without a Bearer token
 * so the backend's AGENT_LOCAL_MODE can accept them.
 */

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080").replace(
  /\/$/,
  ""
);

export type Constituency = {
  slug: string;
  name: string;
  district?: string;
  state?: string;
  seat_type?: string;
  product_name?: string;
  product_tagline?: string;
  coverage?: {
    status?: string;
    voter_available?: boolean;
    completed_booths?: number | null;
    total_booths?: number | null;
    count_basis?: string;
  };
};

export type WebSource = {
  source_id: string;
  title: string;
  url: string;
  published_at: string | null;
  searched_at: string;
  read_status: "page_read" | "snippet_only";
  source_type: string;
};

export type ChatResponse = {
  answer: string;
  charts: unknown[];
  session_id: string;
  agent_path: string[];
  latency_ms?: number;
  status?: "complete" | "partial" | "needs_clarification" | "failed";
  partial_reason?: string | null;
  sources?: WebSource[];
};

export type BoothOption = {
  part_number: string;
  booth_name: string;
  region: string;
  total_voters?: number | null;
};

export type BoothRegion = {
  region: string;
  booths: BoothOption[];
};

export type BoothListResponse = {
  regions: BoothRegion[];
};

export type BoothPortfolioResponse = {
  booth_name?: string | null;
  part_number?: string | null;
  portfolio: Record<string, unknown>;
  source_file?: string | null;
};

async function authHeader(token: string | null): Promise<HeadersInit> {
  const h: HeadersInit = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

export async function listConstituencies(
  token: string | null
): Promise<Constituency[]> {
  const res = await fetch(`${API_URL}/constituencies`, {
    headers: await authHeader(token),
  });
  if (!res.ok) {
    // Fallback for local demo when backend is down
    if (res.status === 0 || res.status >= 500) {
      return [
        {
          slug: "gyanpur",
          name: "Gyanpur",
          district: "Bhadohi",
          state: "Uttar Pradesh",
          product_name: "Arjun",
        },
      ];
    }
    throw new Error(`Failed to load constituencies (${res.status})`);
  }
  return res.json();
}

export async function sendChat(
  question: string,
  constituencyId: string,
  sessionId: string | null,
  token: string | null
): Promise<ChatResponse> {
  const res = await fetch(`${API_URL}/agent/chat`, {
    method: "POST",
    headers: await authHeader(token),
    body: JSON.stringify({
      question,
      constituency_id: constituencyId,
      session_id: sessionId || undefined,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Chat failed (${res.status})`);
  }
  return res.json();
}

export async function listBooths(
  constituencyId: string,
  token: string | null
): Promise<BoothListResponse> {
  const res = await fetch(
    `${API_URL}/portfolio/booths?constituency_id=${encodeURIComponent(
      constituencyId
    )}`,
    { headers: await authHeader(token) }
  );
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to load booths (${res.status})`);
  }
  return res.json();
}

export async function getBoothPortfolio(
  constituencyId: string,
  boothIdentifier: string,
  token: string | null
): Promise<BoothPortfolioResponse | null> {
  const res = await fetch(
    `${API_URL}/portfolio/${encodeURIComponent(
      boothIdentifier
    )}?constituency_id=${encodeURIComponent(constituencyId)}`,
    { headers: await authHeader(token) }
  );
  if (res.status === 404) return null;
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `Failed to load booth report (${res.status})`);
  }
  return res.json();
}

export async function transcribeAudio(
  file: Blob,
  token: string | null
): Promise<{ text: string; language?: string }> {
  const form = new FormData();
  form.append("file", file, "audio.wav");
  const headers: HeadersInit = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_URL}/voice/transcribe`, {
    method: "POST",
    headers,
    body: form,
  });
  if (!res.ok) throw new Error(`Transcription failed (${res.status})`);
  return res.json();
}

export async function synthesizeSpeech(
  text: string,
  token: string | null,
  language?: string
): Promise<Blob> {
  const res = await fetch(`${API_URL}/voice/synthesize`, {
    method: "POST",
    headers: await authHeader(token),
    body: JSON.stringify({ text, language }),
  });
  if (!res.ok) throw new Error(`TTS failed (${res.status})`);
  return res.blob();
}

export type VoterSearchCoverage = {
  availability: "available" | "unavailable" | "unknown";
  completeness: "partial" | "declared" | "unverified";
};

export type VoterSearchOptions = {
  constituency_id: string;
  coverage: VoterSearchCoverage;
  regions: string[];
  booths: {
    booth_id: string;
    part_number: string;
    booth_name: string;
    region: string;
    mapping_status: "mapped" | "unmapped" | "ambiguous";
  }[];
  source_version: string | null;
  booth_list_available: boolean;
};

export type VoterSearchMode = "standard" | "name_only" | "relative_only";
export type VoterSearchField = "name" | "relative_name" | "house_no" | "epic_id";
export type VoterMatchStatus = "exact" | "name_words" | "spelling_variant" | "different" | "not_recorded";
export type VoterSearchRequest = Record<VoterSearchField, string> & {
  constituency_id: string;
  region: string | null;
  booth_id: string | null;
  search_mode: VoterSearchMode;
  spelling_assistance: boolean;
};
export type VoterCandidate = {
  record_id: string;
  voter: Record<VoterSearchField | "serial_no" | "relation_type" | "age" | "gender" | "region" | "polling_station", string | null>;
  booth_id: string | null;
  match_tier: "epic_exact" | "all_details" | "partial_name" | "partial_details" | "single_name";
  field_matches: Partial<Record<VoterSearchField, VoterMatchStatus>>;
  matched_fields: number;
  supplied_fields: number;
};
export type VoterSearchResponse = {
  constituency_id: string;
  region: string | null;
  booth_id: string | null;
  coverage: VoterSearchCoverage;
  source_version: string | null;
  outcome: "matches" | "no_matches" | "refine_search" | "unavailable";
  candidates: VoterCandidate[];
  truncated: boolean;
  duplicate_epic: boolean;
  suggested_modes: Exclude<VoterSearchMode, "standard">[];
  spelling_status: "not_requested" | "used" | "unavailable" | "not_needed";
  spelling_variants: Partial<Record<VoterSearchField, string[]>>;
  request_id: string;
};

export class VoterSearchError extends Error {
  constructor(public code: string, public requestId?: string) {
    super(code);
  }
}

async function voterSearchFetch<T>(path: string, token: string | null, signal: AbortSignal, body?: VoterSearchRequest): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: body ? "POST" : "GET",
    headers: await authHeader(token),
    body: body ? JSON.stringify(body) : undefined,
    signal,
    cache: "no-store",
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    const code = error.error_code || (res.status === 401 ? "unauthorized" : res.status === 403 ? "forbidden" : res.status === 404 ? "api_version_unavailable" : res.status === 422 ? "invalid_input" : "search_unavailable");
    throw new VoterSearchError(code, error.request_id);
  }
  return res.json();
}

export function getVoterSearchOptions(constituencyId: string, token: string | null, signal: AbortSignal) {
  return voterSearchFetch<VoterSearchOptions>(`/voter/search-options?constituency_id=${encodeURIComponent(constituencyId)}`, token, signal);
}

export function findVoters(body: VoterSearchRequest, token: string | null, signal: AbortSignal) {
  return voterSearchFetch<VoterSearchResponse>("/voter/search", token, signal, body);
}
