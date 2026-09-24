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
  sendChat,
  transcribeAudio,
  synthesizeSpeech,
  type Constituency,
  type ChatResponse,
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

  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

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
    setMessages([]);
    setSessionId(null);
    setScreen("login");
  };

  const enterChat = (c: Constituency) => {
    setSelected(c);
    setMessages([]);
    setSessionId(null);
    setScreen("chat");
  };

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
    </div>
  );
}
