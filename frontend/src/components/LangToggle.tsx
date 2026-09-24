"use client";

import type { Lang } from "@/i18n/messages";

type Props = {
  lang: Lang;
  onChange: (l: Lang) => void;
};

export default function LangToggle({ lang, onChange }: Props) {
  return (
    <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 text-sm font-medium">
      <button
        type="button"
        onClick={() => onChange("en")}
        className={`rounded-md px-3 py-1.5 transition ${
          lang === "en"
            ? "bg-navy-900 text-white"
            : "text-slate-600 hover:text-navy-900"
        }`}
      >
        EN
      </button>
      <button
        type="button"
        onClick={() => onChange("hi")}
        className={`rounded-md px-3 py-1.5 transition ${
          lang === "hi"
            ? "bg-navy-900 text-white"
            : "text-slate-600 hover:text-navy-900"
        }`}
      >
        हिं
      </button>
    </div>
  );
}
