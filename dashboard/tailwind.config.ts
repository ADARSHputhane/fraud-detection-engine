import type { Config } from "tailwindcss";

// Every colour is a CSS variable from app/globals.css, so light and dark themes swap in one
// place and components are written against roles, not hex values.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "var(--page)",
        surface: "var(--surface)",
        raised: "var(--raised)",
        ink: "var(--ink)",
        "ink-2": "var(--ink-2)",
        muted: "var(--muted)",
        grid: "var(--grid)",
        axis: "var(--axis)",
        hair: "var(--border)",
        accent: "var(--accent)",
        good: "var(--good)",
        warning: "var(--warning)",
        critical: "var(--critical)",
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
