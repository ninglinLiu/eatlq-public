import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        cream: {
          DEFAULT: "#FAF7F2",
          dark: "#F3EDE4",
        },
        ink: {
          DEFAULT: "#2C2419",
          muted: "#5C5348",
          faint: "#8A8178",
        },
        accent: {
          DEFAULT: "#C4704A",
          muted: "#D4956F",
          soft: "#F0E0D4",
        },
        border: {
          warm: "#E8E0D6",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        serif: ["var(--font-serif)", "Georgia", "serif"],
      },
      maxWidth: {
        prose: "680px",
        content: "900px",
      },
    },
  },
  plugins: [],
};

export default config;
