"use client";

import { useEffect, useState } from "react";

const sections = [
  { id: "abstract", label: "Abstract", level: 1 },
  { id: "architecture", label: "System Architecture", level: 1 },
  { id: "data-pipeline", label: "Data Pipeline", level: 1 },
  { id: "ml-engine", label: "ML Signal Engine", level: 1 },
  { id: "execution", label: "Execution & Risk", level: 1 },
  { id: "eatlq-extensions", label: "EATLQ Extensions", level: 1 },
  { id: "alpha-sop", label: "Alpha Research SOP", level: 1 },
  { id: "dev-methodology", label: "Development Methodology", level: 1 },
  { id: "empirical-results", label: "Empirical Results", level: 1 },
  { id: "lessons", label: "Lessons Learned", level: 1 },
  { id: "roadmap", label: "Roadmap", level: 1 },
];

export default function TableOfContents() {
  const [active, setActive] = useState<string>("abstract");

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActive(entry.target.id);
          }
        }
      },
      { rootMargin: "-20% 0px -70% 0px", threshold: 0 }
    );

    sections.forEach(({ id }) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });

    return () => observer.disconnect();
  }, []);

  return (
    <nav className="w-full">
      <p className="toc-section" style={{ paddingLeft: "0.75rem" }}>
        Contents
      </p>
      {sections.map((s) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          className={`toc-link ${active === s.id ? "active" : ""}`}
          style={{ paddingLeft: s.level === 2 ? "1.5rem" : "0.75rem" }}
        >
          {s.label}
        </a>
      ))}
    </nav>
  );
}
