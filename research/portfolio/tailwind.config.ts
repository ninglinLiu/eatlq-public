import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-inter)", "Inter", "system-ui", "sans-serif"],
        serif: ["var(--font-georgia)", "Georgia", "serif"],
        mono: ["var(--font-mono)", "JetBrains Mono", "Fira Code", "monospace"],
      },
      colors: {
        paper: "#FAFAF7",
        ink: "#1A1A1A",
        accent: {
          DEFAULT: "#C85A00",
          light: "#E87530",
          muted: "#F5E8DA",
        },
        muted: "#6B6B6B",
        border: "#E2E2DC",
        "code-bg": "#F4F4F0",
      },
      typography: {
        DEFAULT: {
          css: {
            color: "#1A1A1A",
            maxWidth: "none",
          },
        },
      },
    },
  },
  plugins: [],
};

export default config;
