# Agent-readable LASZLO whitepapers (v1 + v2)

定向镜像自 WisdomEchoes 博客。目标：Agent **不依赖看图**也能读到图中文字/表格。

## 读什么（优先顺序）

1. `EATLQ_vs_LASZLO_full_comparison_20260719.md` — 与你们路径的全景对照（2026-07-19）
2. `laszlo-whitepaper-v2.md` — v2 全文 + 每张图的 Markdown 转写
3. `laszlo-whitepaper-v1.md` — v1 全文 + 图注转写
4. `FIGURES.md` — 全部配图转写合集（表格/流程优先）
5. `figures/*.md` — 单图转写

交互对照 Canvas：`/root/.cursor/projects/root/canvases/eatlq-vs-laszlo-whitepaper.canvas.tsx`

## 原始资源

- HTML: `../www.wisdomechoes.net/blog/laszlo-whitepaper-v{1,2}.html`
- Images: `../www.wisdomechoes.net/images/blog/laszlo-whitepaper-v{1,2}/`

## 关于图片文字

| 类型 | 数量 | Agent 可读策略 |
|------|------|----------------|
| SVG 信息图 | 多数 | 已从 SVG `<text>` 抽出，转成 Markdown 表格/流程 |
| PNG 封面/概念图 | 4 | 无可读正文；已写视觉摘要，不假装 OCR 出字 |

不要用原始 OCR 结果（PNG 装饰图 OCR 噪声极大）；以本目录 Markdown 为准。
