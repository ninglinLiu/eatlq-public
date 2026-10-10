export type AuthorityLevel =
  | "GOLD"
  | "VALIDATED"
  | "AUTO_CURATED"
  | "ARTIFACT_ONLY"
  | "UNVERIFIED"
  | string;

export type SourcePointer = {
  source_file?: string;
  file?: string;
  transcript_id?: string;
  jsonl_index?: number;
  message_id?: string;
};

export type CanonicalObject = Record<string, unknown> & {
  id?: string;
  object_type?: string;
  statement?: string;
  name?: string;
  decision?: string;
  question?: string;
  authority_level?: AuthorityLevel;
  authorship?: string | string[];
  epistemic_role?: string;
  persistence_scope?: string;
  reuse_policy?: string;
  status?: string;
  source_pointers?: SourcePointer[];
};

export type WindowRecord = {
  window_id: string;
  start: string;
  end: string;
  dir_name: string;
  status: string;
  output_dir?: string;
  empty_classification?: string;
  artifact_fallback?: {
    transcript_user_msgs?: number;
    artifact_count?: number;
    artifact_types?: string[];
  };
};

export type PipelineState = {
  schema_version?: string;
  earliest_research_date?: string;
  latest_research_date?: string;
  TOTAL_REGISTERED_WINDOWS?: number;
  NON_EMPTY_PROCESSED_WINDOWS?: number;
  QA_PASSED_WINDOWS?: number;
  EMPTY_CONFIRMED?: number;
  ARTIFACTS_ONLY?: number;
  QA_PASS_RATE_AMONG_PROCESSED?: string;
  QA_PASSED_SHARE_OF_ALL_WINDOWS?: string;
  global_authority?: string;
  hypotheses_global?: number;
  experiments_global?: number;
};

export type OpenQuestionItem = {
  id: string;
  title: string;
  body: string;
  status: string;
  source: string;
  week?: string;
};

export type KnowledgeCounts = {
  user_theses: number;
  research_principles: number;
  hypotheses: number;
  experiments: number;
  conclusions: number;
  failures: number;
  methods: number;
  decisions: number;
  guardrails: number;
  open_questions: number;
};

export type AuthorityCounts = Record<string, number>;

export type DataMeta = {
  loadedAt: string;
  warnings: string[];
};

export type SearchResult = {
  type: ObjectType;
  id: string;
  title: string;
  snippet: string;
  authority_level?: string;
};

export type ObjectType =
  | "user_thesis"
  | "research_principle"
  | "hypothesis"
  | "experiment"
  | "conclusion"
  | "failure"
  | "method"
  | "decision"
  | "guardrail";

export type WeeklyStats = {
  raw_events?: number;
  selected?: number;
  user_theses?: number;
  research_principles?: number;
  hypotheses?: number;
  experiments?: number;
  conclusions?: number;
  failures?: number;
  methods?: number;
  decisions?: number;
};
