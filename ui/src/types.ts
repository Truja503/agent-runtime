export interface Profile {
  provider: string;
  model: string;
  base_url: string;
  api_key_env: string | null;
  max_tokens: number;
  temperature: number | null;
  timeout_seconds: number;
  retry_count: number;
  structured_output: string;
  local_server: string;
  reasoning_effort?: string | null;
  supports_images?: boolean;
  credential_configured?: boolean;
  location?: string;
}
export interface Configuration {
  profiles: Record<string, Profile>;
  agents: Record<
    string,
    { profile: string; mandate: string | null; max_steps?: number | null }
  >;
  effective_agents?: Record<
    string,
    {
      profile: string;
      provider: string;
      model: string;
      max_steps: number;
      max_tokens: number;
    }
  >;
  source?: string;
  lifecycle?: string;
}
export interface Agent {
  id: string;
  name: string;
  role: string;
  profile: string;
  provider: string;
  model: string;
  location: string;
  endpoint: string;
  max_tokens: number;
  max_steps?: number | null;
  configured_profile?: string;
  configured_model?: string;
  tools: string[];
  risk_limit: string;
  state: string;
  current_task: string | null;
  connection: string;
  mandate: string;
  security_rules: string;
  system_prompt: string;
}
export interface Graph {
  nodes: { id: string; label: string; kind: string; state: string }[];
  edges: { source: string; target: string; state: string }[];
}
export interface WebSearchConfiguration {
  provider: "none" | "searxng";
  searxng_base_url: string;
  configured: boolean;
  internet_enabled: boolean;
}
export interface RuntimeState {
  browser?: { status: string; error: string | null; launch_test?: boolean };
  internet?: {
    enabled: boolean;
    search_provider_configured: boolean;
    search_provider?: string;
    search_endpoint?: string | null;
    recent_requests: { operation: string; status: string; external_data_sent: string[] }[];
    recent_denied: unknown[];
  };
  agents: Agent[];
  graph: Graph;
  project: { name: string; workspace: string };
  prompt_inspection: boolean;
  queued_tasks: number;
}
export interface Task {
  id: string;
  goal: string;
  status: string;
  created_at: string;
  updated_at: string;
  result: Record<string, unknown> | null;
  error: string | null;
  options: { project: string; workspace: string; agent: string; visual_project?: string | null;
    long_run_quality?: boolean; worker_steps_mode?: string; max_repair_cycles?: number | null; project_framework?: string };
}
export interface Workflow {
  cycle_number: number; cycle_limit: number | null; stage: string;
  last_meaningful_progress?: string; files_changed_this_cycle?: string[];
  latest_review?: { verdict: string }; latest_qa?: { screenshots_generated?: boolean;
    previews?: {dom_loaded?: boolean; console_errors?: unknown[]; failed_resources?: unknown[]; horizontal_overflow?: boolean}[] };
  stop_reason?: string;
}
export interface Event {
  id: string;
  type: string;
  timestamp: string;
  actor: string;
  payload: Record<string, unknown>;
}
export interface Evidence {
  tool_calls: Record<string, unknown>[];
  files_modified: string[];
  tests_executed: Record<string, unknown>[];
  verification_actions: Record<string, unknown>[];
  scope: string;
  verified_after_write?: string[];
  reviewer_inspected_files?: string[];
  file_verification?: {
    path: string;
    verified_after_write: boolean;
    reviewer_inspected_complete: boolean;
  }[];
}
export interface Review {
  source_inspection?: string;
  visual_inspection?: string;
  runtime_errors?: string[];
  verdict: string;
  summary: string;
  findings: {
    severity: string;
    category: string;
    message: string;
    affected_files: string[];
    evidence_event_ids: string[];
  }[];
  acceptance_criteria: {
    criterion: string;
    result: string;
    required: boolean;
  }[];
}
export interface Acceptance {
  review_inspection?: string;
  status: string;
  failures: string[];
  checks: { source: string; status: string; message: string }[];
}
