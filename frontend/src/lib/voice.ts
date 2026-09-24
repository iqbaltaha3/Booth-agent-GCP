/**
 * Browser microphone recording helpers.
 * Records audio as a Blob suitable for POST /voice/transcribe.
 */

export type RecorderHandle = {
  stop: () => Promise<Blob>;
  stream: MediaStream;
};

/** Start recording from the default microphone. */
export async function startRecording(): Promise<RecorderHandle> {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("Microphone is not supported in this browser.");
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      channelCount: 1,
    },
  });

  // Prefer formats Sarvam accepts well
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  let mimeType = "";
  for (const c of candidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(c)) {
      mimeType = c;
      break;
    }
  }

  const chunks: BlobPart[] = [];
  const recorder = mimeType
    ? new MediaRecorder(stream, { mimeType })
    : new MediaRecorder(stream);

  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };

  recorder.start(250); // small timeslices so stop is responsive

  return {
    stream,
    stop: () =>
      new Promise<Blob>((resolve, reject) => {
        recorder.onstop = () => {
          stream.getTracks().forEach((t) => t.stop());
          const type = recorder.mimeType || mimeType || "audio/webm";
          resolve(new Blob(chunks, { type }));
        };
        recorder.onerror = () => {
          stream.getTracks().forEach((t) => t.stop());
          reject(new Error("Recording failed"));
        };
        try {
          recorder.stop();
        } catch (e) {
          stream.getTracks().forEach((t) => t.stop());
          reject(e);
        }
      }),
  };
}

/** Play a TTS audio blob; returns a cleanup function to stop playback. */
export function playAudioBlob(blob: Blob): () => void {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.play().catch(() => {});
  const cleanup = () => {
    audio.pause();
    audio.src = "";
    URL.revokeObjectURL(url);
  };
  audio.onended = cleanup;
  return cleanup;
}
