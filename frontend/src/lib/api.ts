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
};

export type ChatResponse = {
  answer: string;
  charts: unknown[];
  session_id: string;
  agent_path: string[];
  latency_ms?: number;
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
