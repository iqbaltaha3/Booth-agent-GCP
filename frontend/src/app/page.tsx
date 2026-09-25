"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  signInWithEmailAndPassword,
  onAuthStateChanged,
  signOut,
  type User,
} from "firebase/auth";
import Logo from "@/components/Logo";
import LangToggle from "@/components/LangToggle";
import { getFirebaseAuth, isDemoMode } from "@/lib/firebase";
import {
  listConstituencies,
  listBooths,
  getBoothPortfolio,
  sendChat,
  transcribeAudio,
  synthesizeSpeech,
  type Constituency,
  type ChatResponse,
  type BoothOption,
  type BoothRegion,
  type BoothPortfolioResponse,
} from "@/lib/api";
import { startRecording, playAudioBlob, type RecorderHandle } from "@/lib/voice";
import { t, type Lang } from "@/i18n/messages";

type Msg = {
  id: string;
  role: "user" | "arjun";
  text: string;
  agentPath?: string[];
  latencyMs?: number;
};

type Screen = "login" | "select" | "chat";
type WorkspaceTab = "chat" | "reports";

type ReportData = Record<string, unknown>;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function formatNumber(value: unknown, suffix = ""): string {
  const n = asNumber(value);
  if (n == null) return "—";
  return `${new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: Number.isInteger(n) ? 0 : 1,
  }).format(n)}${suffix}`;
}

function labelText(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function topEntries(value: unknown, limit = 8): [string, number][] {
  return Object.entries(asRecord(value))
    .map(([key, val]) => [key, asNumber(val)] as [string, number | null])
    .filter((entry): entry is [string, number] => entry[1] != null)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);
}

function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold text-slate-950">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function BarList({
  title,
  entries,
  mode = "count",
}: {
  title: string;
  entries: [string, number][];
  mode?: "count" | "percent";
}) {
  const max = Math.max(...entries.map(([, value]) => value), 1);
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="text-sm font-semibold text-slate-950">{title}</h3>
      <div className="mt-4 space-y-3">
        {entries.length === 0 ? (
          <p className="text-sm text-slate-500">No data available.</p>
        ) : (
          entries.map(([label, value]) => (
            <div key={label}>
              <div className="mb-1 flex items-center justify-between gap-3 text-xs">
                <span className="truncate font-medium text-slate-700">
                  {labelText(label)}
                </span>
                <span className="shrink-0 text-slate-500">
                  {mode === "percent"
                    ? `${value.toFixed(value % 1 === 0 ? 0 : 1)}%`
                    : formatNumber(value)}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full bg-navy-900"
                  style={{ width: `${Math.max(4, (value / max) * 100)}%` }}
                />
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function Donut({
  title,
  entries,
}: {
  title: string;
  entries: [string, number][];
}) {
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  const colors = ["#0b1f3a", "#2f6f73", "#d4a017", "#64748b", "#9f6b3f"];
  let cursor = 0;
  const gradient = entries
    .map(([, value], index) => {
      const start = cursor;
      const end = total > 0 ? cursor + (value / total) * 100 : cursor;
      cursor = end;
      return `${colors[index % colors.length]} ${start}% ${end}%`;
    })
    .join(", ");

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h3 className="text-sm font-semibold text-slate-950">{title}</h3>
      <div className="mt-4 flex items-center gap-5">
        <div
          className="grid h-28 w-28 shrink-0 place-items-center rounded-full"
          style={{
            background: total > 0 ? `conic-gradient(${gradient})` : "#e2e8f0",
          }}
        >
          <div className="grid h-16 w-16 place-items-center rounded-full bg-white text-sm font-semibold text-slate-900">
            {formatNumber(total)}
          </div>
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          {entries.map(([label, value], index) => (
            <div key={label} className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: colors[index % colors.length] }}
                />
                <span className="truncate text-sm text-slate-700">
                  {labelText(label)}
                </span>
              </div>
              <span className="text-sm font-medium text-slate-950">
                {formatNumber(value)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function BoothReport({
  report,
  selectedBooth,
}: {
  report: BoothPortfolioResponse;
  selectedBooth: BoothOption;
}) {
  const data = report.portfolio as ReportData;
  const metadata = asRecord(data.booth_metadata);
  const gender = asRecord(data.gender_demographics);
  const age = asRecord(data.age_demographics);
  const ageStats = asRecord(age.statistics);
  const socio = asRecord(data.socio_religious_profile);
  const household = asRecord(data.household_structure);
  const relation = asRecord(data.family_relation_types);

  const genderEntries = topEntries(gender.counts, 4);
  const ageEntries = topEntries(age.distribution_counts, 8);
  const religionEntries = topEntries(socio.religion_counts, 5);
  const categoryEntries = topEntries(socio.social_category_counts, 6);
  const casteEntries = topEntries(socio.caste_breakdown_counts, 10);
  const familyEntries = topEntries(relation.counts || relation, 8);

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Booth Report
            </p>
            <h2 className="mt-1 text-xl font-semibold text-slate-950">
              {selectedBooth.part_number} · {selectedBooth.booth_name}
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              {selectedBooth.region} region
              {report.source_file ? ` · ${report.source_file}` : ""}
            </p>
          </div>
          <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
            Profile source: {String(metadata.booth_name || selectedBooth.part_number)}
          </div>
        </div>
      </section>

      <div className="grid gap-3 md:grid-cols-4">
        <StatTile
          label="Registered Voters"
          value={formatNumber(metadata.total_registered_voters)}
          hint="From booth analysis"
        />
        <StatTile label="Mean Age" value={formatNumber(ageStats.mean_age)} />
        <StatTile
          label="Median Age"
          value={formatNumber(ageStats.median_age)}
        />
        <StatTile
          label="Sex Ratio"
          value={formatNumber(gender.sex_ratio_females_per_1000_males)}
          hint="Females per 1,000 males"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Donut title="Gender Composition" entries={genderEntries} />
        <BarList title="Age Bands" entries={ageEntries} />
        <Donut title="Religion Profile" entries={religionEntries} />
        <BarList title="Social Categories" entries={categoryEntries} />
        <BarList title="Top Caste Groups" entries={casteEntries} />
        <BarList title="Family Relation Types" entries={familyEntries} />
      </div>

      {Object.keys(household).length > 0 && (
        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <h3 className="text-sm font-semibold text-slate-950">
            Household Structure
          </h3>
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            {Object.entries(household).slice(0, 6).map(([key, value]) => (
              <div key={key} className="rounded-lg bg-slate-50 px-3 py-2">
                <p className="text-xs text-slate-500">{labelText(key)}</p>
                <p className="text-base font-semibold text-slate-950">
                  {typeof value === "object"
                    ? `${Object.keys(asRecord(value)).length} groups`
                    : formatNumber(value)}
                </p>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export default function HomePage() {
  const [lang, setLang] = useState<Lang>("en");
  const [screen, setScreen] = useState<Screen>("login");
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authLoading, setAuthLoading] = useState(false);

  const [constituencies, setConstituencies] = useState<Constituency[]>([]);
  const [selected, setSelected] = useState<Constituency | null>(null);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("chat");

  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const [boothRegions, setBoothRegions] = useState<BoothRegion[]>([]);
  const [boothsLoading, setBoothsLoading] = useState(false);
  const [boothError, setBoothError] = useState("");
  const [selectedPart, setSelectedPart] = useState("");
  const [report, setReport] = useState<BoothPortfolioResponse | null>(null);
  const [reportMissing, setReportMissing] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);

  // Voice state
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const recorderRef = useRef<RecorderHandle | null>(null);
  const stopPlaybackRef = useRef<(() => void) | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const demo = isDemoMode();

  // Auth listener
  useEffect(() => {
    if (demo) {
      setUser({ uid: "demo", email: "demo@local" } as User);
      setToken(null);
      setScreen("select");
      return;
    }
    const auth = getFirebaseAuth();
    const unsub = onAuthStateChanged(auth, async (u) => {
      setUser(u);
      if (u) {
        const tkn = await u.getIdToken();
        setToken(tkn);
        setScreen((s) => (s === "login" ? "select" : s));
      } else {
        setToken(null);
        setScreen("login");
        setSelected(null);
      }
    });
    return () => unsub();
  }, [demo]);

  // Load constituencies
  useEffect(() => {
    if (screen !== "select" && screen !== "chat") return;
    listConstituencies(token)
      .then(setConstituencies)
      .catch(() =>
        setConstituencies([
          {
            slug: "gyanpur",
            name: "Gyanpur",
            district: "Bhadohi",
            state: "Uttar Pradesh",
            product_name: "Arjun",
          },
        ])
      );
  }, [screen, token]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending, recording, transcribing]);

  // Cleanup mic / audio on unmount
  useEffect(() => {
    return () => {
      stopPlaybackRef.current?.();
      recorderRef.current?.stream.getTracks().forEach((tr) => tr.stop());
    };
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError("");
    setAuthLoading(true);
    try {
      if (demo) {
        setScreen("select");
        return;
      }
      const auth = getFirebaseAuth();
      await signInWithEmailAndPassword(auth, email.trim(), password);
    } catch {
      setAuthError(
        lang === "hi"
          ? "ईमेल या पासवर्ड गलत है।"
          : "Invalid email or password."
      );
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = async () => {
    stopPlaybackRef.current?.();
    if (!demo) await signOut(getFirebaseAuth());
    setSelected(null);
    setActiveTab("chat");
    setMessages([]);
    setSessionId(null);
    setBoothRegions([]);
    setSelectedPart("");
    setReport(null);
    setReportMissing(false);
    setScreen("login");
  };

  const enterChat = (c: Constituency) => {
    setSelected(c);
    setActiveTab("chat");
    setMessages([]);
    setSessionId(null);
    setBoothRegions([]);
    setSelectedPart("");
    setReport(null);
    setReportMissing(false);
    setScreen("chat");
  };

  useEffect(() => {
    if (!selected || screen !== "chat" || activeTab !== "reports") return;
    setBoothsLoading(true);
    setBoothError("");
    listBooths(selected.slug, token)
      .then((res) => setBoothRegions(res.regions))
      .catch((err) =>
        setBoothError(
          err instanceof Error ? err.message : "Failed to load booth list."
        )
      )
      .finally(() => setBoothsLoading(false));
  }, [activeTab, screen, selected, token]);

  const allBooths = boothRegions.flatMap((region) => region.booths);
  const selectedBooth =
    allBooths.find((booth) => booth.part_number === selectedPart) || null;

  const loadReport = useCallback(
    async (partNumber: string) => {
      if (!selected || !partNumber) return;
      setReport(null);
      setReportMissing(false);
      setBoothError("");
      setReportLoading(true);
      try {
        const res = await getBoothPortfolio(selected.slug, partNumber, token);
        if (res) setReport(res);
        else setReportMissing(true);
      } catch (err) {
        setBoothError(
          err instanceof Error ? err.message : "Failed to load booth report."
        );
      } finally {
        setReportLoading(false);
      }
    },
    [selected, token]
  );

  const ask = useCallback(
    async (question: string) => {
      if (!question.trim() || !selected || sending) return;
      setError("");
      const q = question.trim();
      setInput("");
      const userMsg: Msg = {
        id: `u-${Date.now()}`,
        role: "user",
        text: q,
      };
      setMessages((m) => [...m, userMsg]);
      setSending(true);
      try {
        const res: ChatResponse = await sendChat(
          q,
          selected.slug,
          sessionId,
          token
        );
        setSessionId(res.session_id);
        setMessages((m) => [
          ...m,
          {
            id: `a-${Date.now()}`,
            role: "arjun",
            text: res.answer,
            agentPath: res.agent_path,
            latencyMs: res.latency_ms,
          },
        ]);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : t(lang, "errorGeneric")
        );
      } finally {
        setSending(false);
      }
    },
    [selected, sending, sessionId, token, lang]
  );

  /** Toggle microphone: start recording or stop + transcribe */
  const toggleMic = async () => {
    setError("");
    if (recording && recorderRef.current) {
      // Stop and send to STT
      setRecording(false);
      setTranscribing(true);
      try {
        const blob = await recorderRef.current.stop();
        recorderRef.current = null;
        const result = await transcribeAudio(blob, token);
        const text = (result.text || "").trim();
        if (text) {
          setInput((prev) => (prev ? `${prev} ${text}` : text));
        } else {
          setError(t(lang, "voiceError"));
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : "";
        if (msg.toLowerCase().includes("permission") || msg.toLowerCase().includes("not allowed")) {
          setError(t(lang, "micDenied"));
        } else {
          setError(t(lang, "voiceError"));
        }
      } finally {
        setTranscribing(false);
      }
      return;
    }

    // Start recording
    try {
      const handle = await startRecording();
      recorderRef.current = handle;
      setRecording(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      if (msg.toLowerCase().includes("permission") || msg.toLowerCase().includes("not allowed")) {
        setError(t(lang, "micDenied"));
      } else if (msg.toLowerCase().includes("not supported")) {
        setError(t(lang, "micUnsupported"));
      } else {
        setError(t(lang, "voiceError"));
      }
    }
  };

  /** Play TTS for an Arjun message */
  const playAnswer = async (msg: Msg) => {
    if (playingId === msg.id) {
      stopPlaybackRef.current?.();
      stopPlaybackRef.current = null;
      setPlayingId(null);
      return;
    }
    stopPlaybackRef.current?.();
    setPlayingId(msg.id);
    setError("");
    try {
      const ttsLang = lang === "hi" ? "hi-IN" : "en-IN";
      const blob = await synthesizeSpeech(msg.text, token, ttsLang);
      stopPlaybackRef.current = playAudioBlob(blob);
      // Clear playing state when audio ends (playAudioBlob cleans up on end)
      setTimeout(() => {
        // Fallback clear if onended already ran
      }, 0);
      // Attach a listener by re-wrapping is hard; use a short poll on the cleanup
      const check = setInterval(() => {
        // When user clicks stop, playingId is cleared; when natural end, cleanup runs
      }, 500);
      // Better: playAudioBlob already calls cleanup onended — we need to clear playingId
      // Patch: recreate with callback
      stopPlaybackRef.current?.();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = () => {
        URL.revokeObjectURL(url);
        setPlayingId(null);
        stopPlaybackRef.current = null;
        clearInterval(check);
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        setPlayingId(null);
        clearInterval(check);
      };
      stopPlaybackRef.current = () => {
        audio.pause();
        audio.src = "";
        URL.revokeObjectURL(url);
        clearInterval(check);
      };
      await audio.play();
    } catch {
      setPlayingId(null);
      setError(t(lang, "voiceError"));
    }
  };

  const copyText = (text: string) => {
    navigator.clipboard?.writeText(text).catch(() => {});
  };

  const shareText = async (text: string) => {
    if (navigator.share) {
      try {
        await navigator.share({ text });
      } catch {
        /* cancelled */
      }
    } else {
      copyText(text);
    }
  };

  // ——— LOGIN ———
  if (screen === "login") {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-surface px-4">
        <div className="absolute right-4 top-4">
          <LangToggle lang={lang} onChange={setLang} />
        </div>
        <div className="w-full max-w-sm">
          <div className="mb-8 flex flex-col items-center text-center">
            <div className="mb-4 rounded-full bg-navy-950 p-4 shadow-lg">
              <Logo size={72} />
            </div>
            <h1 className="text-2xl font-bold text-navy-950">
              {t(lang, "appName")}
            </h1>
            <p className="mt-1 text-lg font-medium text-gold">
              {t(lang, "taglineNative")}
            </p>
            {demo && (
              <p className="mt-3 rounded-lg bg-amber-50 px-3 py-1.5 text-xs text-amber-800">
                {t(lang, "demoMode")}
              </p>
            )}
          </div>
          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">
                {t(lang, "email")}
              </label>
              <input
                type="email"
                className="input-field"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required={!demo}
                placeholder="you@example.com"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-700">
                {t(lang, "password")}
              </label>
              <input
                type="password"
                className="input-field"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required={!demo}
              />
            </div>
            {authError && (
              <p className="text-sm text-red-600">{authError}</p>
            )}
            <button
              type="submit"
              className="btn-primary w-full"
              disabled={authLoading}
            >
              {authLoading ? t(lang, "signingIn") : t(lang, "signIn")}
            </button>
          </form>
        </div>
      </div>
    );
  }

  // ——— SELECT CONSTITUENCY ———
  if (screen === "select") {
    return (
      <div className="min-h-screen bg-surface">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="rounded-full bg-navy-950 p-1.5">
              <Logo size={36} />
            </div>
            <div>
              <p className="text-sm font-bold text-navy-950">
                {t(lang, "appName")}
              </p>
              <p className="text-xs text-gold">{t(lang, "taglineNative")}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <LangToggle lang={lang} onChange={setLang} />
            <button
              type="button"
              onClick={handleLogout}
              className="text-sm text-slate-500 hover:text-navy-900"
            >
              {t(lang, "logout")}
            </button>
          </div>
        </header>
        <main className="mx-auto max-w-lg px-4 py-8">
          <h2 className="text-xl font-bold text-navy-950">
            {t(lang, "selectSeat")}
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            {t(lang, "selectSeatHint")}
          </p>
          <ul className="mt-6 space-y-3">
            {constituencies.map((c) => (
              <li key={c.slug}>
                <button
                  type="button"
                  onClick={() => enterChat(c)}
                  className="btn-secondary flex w-full items-center justify-between text-left"
                >
                  <div>
                    <p className="font-semibold text-navy-950">{c.name}</p>
                    <p className="text-sm text-slate-500">
                      {[c.district, c.state].filter(Boolean).join(", ")}
                    </p>
                  </div>
                  <span className="text-gold">→</span>
                </button>
              </li>
            ))}
          </ul>
        </main>
      </div>
    );
  }

  // ——— CHAT ———
  const examples = [
    t(lang, "example1"),
    t(lang, "example2"),
    t(lang, "example3"),
    t(lang, "example4"),
  ];

  return (
    <div className="flex h-screen flex-col bg-surface">
      <header className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-white px-3 py-2.5">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setScreen("select")}
            className="rounded-lg p-2 text-slate-500 hover:bg-slate-100"
            aria-label={t(lang, "back")}
          >
            ←
          </button>
          <div className="rounded-full bg-navy-950 p-1">
            <Logo size={28} />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-bold text-navy-950">
              Arjun · {selected?.name}
            </p>
            <p className="truncate text-xs text-slate-500">
              {[selected?.district, selected?.state].filter(Boolean).join(", ")}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <LangToggle lang={lang} onChange={setLang} />
          <button
            type="button"
            onClick={handleLogout}
            className="text-xs text-slate-500 hover:text-navy-900"
          >
            {t(lang, "logout")}
          </button>
        </div>
      </header>

      <nav className="shrink-0 border-b border-slate-200 bg-white px-3">
        <div className="mx-auto flex max-w-5xl gap-1">
          {(["chat", "reports"] as WorkspaceTab[]).map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`border-b-2 px-4 py-3 text-sm font-semibold transition ${
                activeTab === tab
                  ? "border-navy-900 text-navy-950"
                  : "border-transparent text-slate-500 hover:text-slate-900"
              }`}
            >
              {tab === "chat" ? "Chat" : "Booth Reports"}
            </button>
          ))}
        </div>
      </nav>

      {activeTab === "chat" ? (
        <>
      <div className="flex-1 overflow-y-auto px-3 py-4">
        {messages.length === 0 && (
          <div className="mx-auto max-w-lg py-6 text-center">
            <div className="mx-auto mb-4 w-fit rounded-full bg-navy-950 p-3">
              <Logo size={48} />
            </div>
            <p className="text-lg font-semibold text-navy-950">
              {t(lang, "taglineNative")}
            </p>
            <p className="mt-4 text-left text-sm font-medium text-slate-600">
              {t(lang, "examples")}
            </p>
            <ul className="mt-2 space-y-2 text-left">
              {examples.map((ex) => (
                <li key={ex}>
                  <button
                    type="button"
                    onClick={() => ask(ex)}
                    className="w-full rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-sm text-navy-900 shadow-sm transition hover:border-gold"
                  >
                    {ex}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mx-auto max-w-2xl space-y-4">
          {messages.map((m) => (
            <div
              key={m.id}
              className={m.role === "user" ? "bubble-user" : "bubble-arjun"}
            >
              <div className="whitespace-pre-wrap text-[15px] leading-relaxed">
                {m.text}
              </div>
              {m.role === "arjun" && (
                <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-2">
                  <button
                    type="button"
                    onClick={() => playAnswer(m)}
                    className={`text-xs font-medium ${
                      playingId === m.id
                        ? "text-gold"
                        : "text-slate-500 hover:text-navy-900"
                    }`}
                  >
                    {playingId === m.id
                      ? t(lang, "speaking")
                      : `🔊 ${t(lang, "listen")}`}
                  </button>
                  <button
                    type="button"
                    onClick={() => copyText(m.text)}
                    className="text-xs font-medium text-slate-500 hover:text-navy-900"
                  >
                    {t(lang, "copy")}
                  </button>
                  <button
                    type="button"
                    onClick={() => shareText(m.text)}
                    className="text-xs font-medium text-slate-500 hover:text-navy-900"
                  >
                    {t(lang, "share")}
                  </button>
                  {m.agentPath && m.agentPath.length > 0 && (
                    <details className="text-xs text-slate-400">
                      <summary className="cursor-pointer hover:text-slate-600">
                        {t(lang, "evidence")}
                      </summary>
                      <p className="mt-1 pl-2">
                        {m.agentPath.join(" → ")}
                        {m.latencyMs != null && ` · ${m.latencyMs} ms`}
                      </p>
                    </details>
                  )}
                </div>
              )}
            </div>
          ))}
          {sending && (
            <div className="bubble-arjun text-sm text-slate-500">
              {t(lang, "thinking")}
            </div>
          )}
          {transcribing && (
            <div className="text-center text-sm text-slate-500">
              {t(lang, "transcribing")}
            </div>
          )}
          {error && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </p>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Input bar — thumb zone: mic + text + send */}
      <div className="shrink-0 border-t border-slate-200 bg-white px-3 pt-2 pb-safe">
        {recording && (
          <p className="mb-1 text-center text-sm font-medium text-red-600 animate-pulse">
            ● {t(lang, "listening")}
          </p>
        )}
        <form
          className="mx-auto flex max-w-2xl items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            ask(input);
          }}
        >
          {/* Mic button — large, thumb-friendly */}
          <button
            type="button"
            onClick={toggleMic}
            disabled={sending || transcribing}
            aria-label={recording ? t(lang, "stopListening") : t(lang, "listening")}
            className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full text-xl shadow-sm transition active:scale-95 disabled:opacity-50 ${
              recording
                ? "bg-red-600 text-white"
                : "border border-slate-200 bg-white text-navy-900 hover:border-gold"
            }`}
          >
            {recording ? "⏹" : "🎤"}
          </button>

          <textarea
            rows={1}
            className="input-field max-h-32 min-h-[48px] flex-1 resize-none"
            placeholder={t(lang, "askPlaceholder")}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                ask(input);
              }
            }}
            disabled={sending || recording}
          />
          <button
            type="submit"
            className="btn-primary shrink-0 px-4"
            disabled={sending || recording || !input.trim()}
          >
            {t(lang, "send")}
          </button>
        </form>
        <p className="mx-auto mt-1 max-w-2xl pb-2 text-center text-[10px] text-slate-400">
          {selected?.name} · {t(lang, "taglineNative")}
        </p>
      </div>
        </>
      ) : (
        <div className="flex-1 overflow-y-auto bg-slate-50 px-4 py-5">
          <div className="mx-auto max-w-6xl space-y-5">
            <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    {selected?.name} Constituency
                  </p>
                  <h1 className="mt-1 text-2xl font-semibold text-slate-950">
                    Booth Reports
                  </h1>
                  <p className="mt-1 max-w-2xl text-sm text-slate-500">
                    Select a region and booth from this constituency only. Reports are read from the selected constituency&apos;s booth analysis folder.
                  </p>
                </div>
                <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
                  {boothRegions.length} regions · {allBooths.length} booths
                </div>
              </div>
            </section>

            {boothsLoading && (
              <div className="rounded-lg border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
                Loading booth list...
              </div>
            )}

            {boothError && (
              <div className="rounded-lg border border-red-100 bg-red-50 p-4 text-sm text-red-700">
                {boothError}
              </div>
            )}

            {!boothsLoading && boothRegions.length > 0 && (
              <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                <label className="block text-sm font-semibold text-slate-950">
                  Choose booth / part number
                </label>
                <select
                  className="mt-2 w-full rounded-lg border border-slate-200 bg-white px-3 py-3 text-sm text-slate-900 outline-none ring-gold/40 focus:border-gold focus:ring-2"
                  value={selectedPart}
                  onChange={(event) => {
                    const next = event.target.value;
                    setSelectedPart(next);
                    if (next) loadReport(next);
                    else {
                      setReport(null);
                      setReportMissing(false);
                    }
                  }}
                >
                  <option value="">Select a booth</option>
                  {boothRegions.map((region) => (
                    <optgroup key={region.region} label={labelText(region.region)}>
                      {region.booths.map((booth) => (
                        <option key={booth.part_number} value={booth.part_number}>
                          {booth.part_number} · {booth.booth_name}
                          {booth.total_voters ? ` · ${formatNumber(booth.total_voters)} voters` : ""}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                {selectedBooth && (
                  <div className="mt-3 grid gap-2 text-sm text-slate-600 md:grid-cols-3">
                    <div className="rounded-lg bg-slate-50 px-3 py-2">
                      <span className="font-medium text-slate-900">Region:</span>{" "}
                      {labelText(selectedBooth.region)}
                    </div>
                    <div className="rounded-lg bg-slate-50 px-3 py-2">
                      <span className="font-medium text-slate-900">Part:</span>{" "}
                      {selectedBooth.part_number}
                    </div>
                    <div className="rounded-lg bg-slate-50 px-3 py-2">
                      <span className="font-medium text-slate-900">Boothlist voters:</span>{" "}
                      {formatNumber(selectedBooth.total_voters)}
                    </div>
                  </div>
                )}
              </section>
            )}

            {reportLoading && (
              <div className="rounded-lg border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
                Preparing report...
              </div>
            )}

            {reportMissing && selectedBooth && (
              <div className="rounded-lg border border-slate-200 bg-white p-8 text-center shadow-sm">
                <p className="text-lg font-semibold text-slate-950">
                  This Booth&apos;s data will soon arrive.
                </p>
                <p className="mt-2 text-sm text-slate-500">
                  {selectedBooth.part_number} · {selectedBooth.booth_name}
                </p>
              </div>
            )}

            {report && selectedBooth && (
              <BoothReport report={report} selectedBooth={selectedBooth} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
