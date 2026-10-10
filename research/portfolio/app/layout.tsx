import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "EATLQ — On-Chain DeFi Intelligence System | Engineering Portfolio",
  description:
    "Transparent architecture report for a production-grade, ML-driven on-chain trading intelligence system targeting Base L2 DeFi markets. Covers system design, empirical results, and engineering methodology.",
  openGraph: {
    title: "EATLQ Engineering Portfolio",
    description:
      "A transparent whitepaper on building a multi-service, ML-driven DeFi intelligence system from chain ingestion to execution.",
    type: "article",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="bg-paper text-ink antialiased">{children}</body>
    </html>
  );
}
