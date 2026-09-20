export interface Profile {
  provider: string; model: string; base_url: string; api_key_env: string | null;
  max_tokens: number; temperature: number | null; timeout_seconds: number; retry_count: number;
  structured_output: string; local_server: string; credential_configured?: boolean; location?: string;
}
export interface Configuration { profiles: Record<string, Profile>; agents: Record<string, {profile: string; mandate: string | null}> }
export interface Agent { id: string; name: string; role: string; profile: string; provider: string; model: string; location: string; endpoint: string; max_tokens: number; tools: string[]; risk_limit: string; state: string; current_task: string | null; connection: string; mandate: string; security_rules: string; system_prompt: string }
export interface Graph {nodes: {id: string; label: string; kind: string; state: string}[]; edges: {source: string; target: string; state: string}[]}
export interface RuntimeState {agents: Agent[]; graph: Graph; project: {name: string; workspace: string}; prompt_inspection: boolean; queued_tasks: number}
export interface Task {id: string; goal: string; status: string; created_at: string; updated_at: string; result: Record<string, unknown> | null; error: string | null; options: {project: string; workspace: string; agent: string}}
export interface Event {id: string; type: string; timestamp: string; actor: string; payload: Record<string, unknown>}
export interface Evidence {tool_calls: Record<string, unknown>[]; files_modified: string[]; tests_executed: Record<string, unknown>[]; verification_actions: Record<string, unknown>[]; scope: string}
