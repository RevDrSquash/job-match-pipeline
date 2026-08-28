export type SkillRef = {
  id: string;
  label: string;
};

export type User = {
  id: string;
  tier: string;
  quota_remaining: number;
  quota_reset_at: string | null;
};

export type MatchUiState = {
  viewed: boolean;
  skipped: {
    reason_code: string | null;
    reason_text: string | null;
  } | null;
  generate_requested: boolean;
  applied_at: string | null;
  outcome: "interview" | "rejected" | null;
};

export type Match = {
  id: string;
  job_id: string;
  title: string | null;
  company: string | null;
  location: string | null;
  comp_min: number | null;
  comp_max: number | null;
  posted_at: string | null;
  rerank_score: number | null;
  qualification_label:
    | "unqualified"
    | "minimally_qualified"
    | "overqualified"
    | "potentially_qualified"
    | "clearly_qualified"
    | null;
  screen_reason: string | null;
  matched_skills: SkillRef[];
  adjacent_skills: SkillRef[];
  missing_skills: SkillRef[];
  generation_id: string | null;
  ui: MatchUiState;
};

export type WorkBullet = {
  span_id?: string;
  text?: string;
};

export type WorkHistoryEntry = {
  employer?: string;
  title?: string;
  start_date?: string | null;
  end_date?: string | null;
  is_current?: boolean;
  location?: string | null;
  source?: string;
  bullets?: Array<WorkBullet | string>;
  [key: string]: unknown;
};

export type ProfileFilters = {
  title_families: string[] | null;
  locations: string[] | null;
  comp_floor: number | null;
  seniority_band: string | null;
  work_arrangement: string[] | null;
};

export type Profile = {
  user_id: string;
  profile_version: number;
  rematch_needed: boolean;
  work_history: WorkHistoryEntry[];
  skill_ids: string[];
  skills: SkillRef[];
  synthesized_doc: string | null;
  embedding_dim: number | null;
  filters: ProfileFilters;
  rescan_message?: string;
};

export type Generation = {
  id: string;
  match_id: string;
  user_id: string;
  resume_doc: string | null;
  claim_source_map: Record<string, unknown> | null;
  verify_status: string | null;
  verify_failures: unknown[];
  job: {
    id: string;
    title: string | null;
    company: string | null;
    location: string | null;
    url: string;
    comp_min: number | null;
    comp_max: number | null;
  };
  match: {
    rerank_score: number | null;
    qualification_label: string | null;
    screen_reason: string | null;
    matched_skills: SkillRef[];
    adjacent_skills: SkillRef[];
    missing_skills: SkillRef[];
  };
  ui: MatchUiState;
};

export type JobSummary = {
  id: string;
  title: string | null;
  company: string | null;
  location: string | null;
  comp_min: number | null;
  comp_max: number | null;
  posted_at: string | null;
  extracted_at: string | null;
};

export type JobDetail = JobSummary & {
  url: string | null;
  raw_jd: string | null;
  raw_jd_html: string | null;
  seniority: string | null;
  hard_requirements: string[];
  nice_to_haves: string[];
};

export type SkillStats = {
  concepts_by_type: Record<string, number>;
  aliases_by_type: Record<string, number>;
  source_concepts: Array<{
    source: string;
    source_version: string;
    count: number;
  }>;
  edges: {
    canonical: number;
    source: number;
  };
};

export type SkillSearchHit = {
  id: string;
  label: string;
  concept_type: string;
  matched_alias: string;
};

export type SkillSourceRef = {
  source: string;
  source_version: string;
  external_id: string;
  name: string;
  mapping_type: string;
  mapping_method: string;
  confidence: number;
};

export type SkillDetail = {
  id: string;
  canonical_name: string;
  normalized_name: string;
  concept_type: string;
  status: string;
  description: string | null;
  aliases: Record<string, string[]>;
  sources: SkillSourceRef[];
};

export type SkillGraphNode = {
  id: string;
  label: string;
  concept_type: string;
  layer: "canonical" | "source";
  member_count?: number;
};

export type SkillGraphEdge = {
  source: string;
  target: string;
  predicate: string;
  layer: "canonical" | "source";
  confidence: number;
};

export type SkillGraphPayload = {
  nodes: SkillGraphNode[];
  edges: SkillGraphEdge[];
  truncated: boolean;
};

export type AdminMetrics = {
  collected_at: string;
  funnel: Record<string, number>;
  extraction_coverage: number | null;
  label_distribution: Record<string, number>;
  llm_spend_usd: number;
  usage_by_stage: Record<
    string,
    {
      n: number | null;
      cost_usd_total: number | null;
    }
  >;
};
