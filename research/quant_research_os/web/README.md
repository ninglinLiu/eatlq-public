# Quant Research OS — Read-only Knowledge Browser

A bilingual (English / 中文) read-only web frontend for browsing Quant Research OS v1 compiled knowledge.

## Stack

- **Next.js 14** (App Router)
- **TypeScript**
- **Tailwind CSS 3**
- **react-markdown** + remark-gfm for Markdown rendering

## Data sources (read-only)

The server reads directly from the canonical OS tree — no data copy:

```text
/root/quant_research_os/global/*.jsonl
/root/quant_research_os/global/*.md
/root/quant_research_os/weekly/
/root/quant_research_os/monthly/
/root/quant_research_os/state/
/root/quant_research_os/AUTHORITY_MODEL.md
/root/quant_research_os/PRODUCTION_STATUS.md
```

Paths outside this allowlist are blocked. Original knowledge files are never modified.

## Pages

| Route | Description |
|-------|-------------|
| `/en` or `/zh` | Home / Overview |
| `/…/conclusions` | Conclusions with filters |
| `/…/hypotheses` | Hypotheses |
| `/…/experiments` | Experiments (dedup notice) |
| `/…/failures` | Failures & Permanent Guardrails tabs |
| `/…/decisions` | Decisions (execution status distinction) |
| `/…/theses` | User Theses |
| `/…/principles` | Research Principles |
| `/…/methods` | Methods |
| `/…/timeline` | Weekly windows |
| `/…/timeline/[windowId]` | Window detail (summary, QA, open questions) |
| `/…/monthly` | Monthly summaries |
| `/…/open-questions` | Unresolved questions |
| `/…/current-state` | current_state.md reader |
| `/…/authority` | AUTHORITY_MODEL.md reader |
| `/…/production-status` | PRODUCTION_STATUS.md reader |
| `/…/search?q=` | Global search |
| `/…/object/[type]/[id]` | Canonical object detail |

Switch language via sidebar footer: **English ↔ 中文** (preserves current path).

## Quick start

```bash
cd /root/quant_research_os/web
npm install
npm run build
npm run start
```

Default bind: **127.0.0.1:3000**

Development:

```bash
npm run dev
```

If port 3000 is busy, use another port:

```bash
npm run start -- -p 3001
# or
npm run dev -- -p 3001
```

## SSH tunnel (remote VPS)

From your local machine:

```bash
ssh -L 3000:127.0.0.1:3000 user@your-vps-host
```

Then open: http://127.0.0.1:3000/en

## Security

- Read-only — no write API, no shell execution
- No `.env` or secrets exposed
- No arbitrary file read — allowlisted paths only
- Binds to localhost by default (not public)

## Known limitations (v1)

- **Experiments count (353)** is provisional — may include duplicates from auto-promotion
- **open_questions.jsonl** global file is empty — questions aggregated from weekly `open_questions.md`
- **No knowledge graph visualization** — graph JSONL loaded but not rendered
- **No experiment deduplication UI**
- **Filter forms** use GET submit (full page reload)
- **60s in-memory cache** for JSONL reads
- **Round 3 GOLD** objects not separately tagged in global pack UI filter

## Architecture

```text
web/
├── app/[locale]/          # Localized routes (en, zh)
├── components/            # UI, layout, pages
├── lib/
│   ├── data/              # loader, paths, filters, types
│   ├── i18n/              # EN/ZH dictionaries
│   └── utils/             # format, object helpers
└── README.md
```

## Next version priorities

1. Experiment deduplication view + true unique count
2. Knowledge graph related-object links on detail pages
3. Round 3 GOLD authority tier in global pack
4. Client-side filter without full reload
5. ARTIFACTS_ONLY week curation preview
