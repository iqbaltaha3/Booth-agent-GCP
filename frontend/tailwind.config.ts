import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        navy: {
          950: "#0B1F3A",
          900: "#1A365D",
          800: "#1E3A5F",
          700: "#2A4A73",
        },
        gold: {
          DEFAULT: "#D4A017",
          soft: "#E8C547",
          muted: "#B8860B",
        },
        surface: {
          DEFAULT: "#F8FAFC",
          2: "#F1F5F9",
        },
      },
      fontFamily: {
        sans: [
          "Noto Sans",
          "Noto Sans Devanagari",
          "system-ui",
          "sans-serif",
        ],
      },
      minHeight: {
        touch: "44px",
      },
    },
  },
  plugins: [],
};

export default config;
