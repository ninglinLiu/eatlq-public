import path from "path";

/** Canonical Quant Research OS data root — read-only. */
export const OS_ROOT = path.resolve(process.cwd(), "..");

export const PATHS = {
  global: path.join(OS_ROOT, "global"),
  weekly: path.join(OS_ROOT, "weekly"),
  monthly: path.join(OS_ROOT, "monthly"),
  state: path.join(OS_ROOT, "state"),
  readme: path.join(OS_ROOT, "README.md"),
  authorityModel: path.join(OS_ROOT, "AUTHORITY_MODEL.md"),
  productionStatus: path.join(OS_ROOT, "PRODUCTION_STATUS.md"),
} as const;

/** Allowed path prefixes for markdown reads. */
export const ALLOWED_READ_PREFIXES = [
  PATHS.global,
  PATHS.weekly,
  PATHS.monthly,
  PATHS.state,
  OS_ROOT,
] as const;

export function isAllowedPath(filePath: string): boolean {
  const resolved = path.resolve(filePath);
  return ALLOWED_READ_PREFIXES.some(
    (prefix) => resolved === prefix || resolved.startsWith(prefix + path.sep)
  );
}

export const GLOBAL_JSONL = {
  user_theses: "user_theses.jsonl",
  research_principles: "research_principles.jsonl",
  hypotheses: "hypotheses.jsonl",
  experiments: "experiments.jsonl",
  conclusions: "conclusions.jsonl",
  failures: "failures.jsonl",
  methods: "methods.jsonl",
  decisions: "decisions.jsonl",
  open_questions: "open_questions.jsonl",
  permanent_guardrails: "permanent_guardrails.jsonl",
  knowledge_graph: "knowledge_graph.jsonl",
} as const;

export type ObjectType =
  | "user_thesis"
  | "research_principle"
  | "hypothesis"
  | "experiment"
  | "conclusion"
  | "failure"
  | "method"
  | "decision"
  | "guardrail"
  | "open_question";

export const TYPE_TO_FILE: Record<ObjectType, string> = {
  user_thesis: GLOBAL_JSONL.user_theses,
  research_principle: GLOBAL_JSONL.research_principles,
  hypothesis: GLOBAL_JSONL.hypotheses,
  experiment: GLOBAL_JSONL.experiments,
  conclusion: GLOBAL_JSONL.conclusions,
  failure: GLOBAL_JSONL.failures,
  method: GLOBAL_JSONL.methods,
  decision: GLOBAL_JSONL.decisions,
  guardrail: GLOBAL_JSONL.permanent_guardrails,
  open_question: GLOBAL_JSONL.open_questions,
};
