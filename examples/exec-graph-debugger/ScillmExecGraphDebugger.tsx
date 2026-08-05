import React, { useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";
import {
  analyzeExecGraphRuntimeReadiness,
  applyNicoPlanProposal,
  applyPlanPatch,
  cloneExecGraph,
  diffExecGraphPlan,
  validateExecGraphPlan,
  type NicoPlanProposal,
  type PlanDiffItem,
  type PlanValidationIssue,
  type PlanValidationResult,
  type RuntimeReadinessNodeReport,
  type RuntimeReadinessReport,
} from "./execGraphPlanEditor";

export type ExecNodeState =
  | "pending"
  | "ready"
  | "queued"
  | "running"
  | "paused"
  | "passed"
  | "needs_attention"
  | "failed"
  | "skipped"
  | "stopped";

export type ExecGraphNode = {
  id: string;
  type: string;
  node_goal: string;
  depends_on?: string[];
  protocol_role?: string;
  persona_ref?: string;
  model?: string;
  prompt?: string;
  review_scopes?: ReviewScopeSpec[];
  messages?: Array<Record<string, unknown>>;
  output_schema?: Record<string, unknown>;
  template_id?: string;
  template_version?: string;
  template_sha256?: string;
  catalog_id?: string;
  catalog_version?: string;
  catalog_sha256?: string;
  inline_overrides?: Record<string, unknown>;
  human_questions?: InterviewNodeQuestion[];
  recommendation?: string;
  reason?: string;
};

export type InterviewNodeQuestion = {
  id: string;
  header?: string;
  text: string;
  options?: Array<{ label: string; description?: string } | string>;
  multi_select?: boolean;
  recommendation?: string;
  reason?: string;
  images?: string[];
  allow_custom_image?: boolean;
};

export type ReviewScopeSpec = {
  scope?: string;
  contract?: string;
  agent?: string;
  model?: string;
  review_level?: "default" | "risk_expanded" | "adversarial" | "proof_gapfill" | string;
  proof_level?: "proven" | "static_confirmed" | "likely" | "speculative" | string;
  reducer_policy?: string;
  read_only?: boolean;
  evidence_required?: boolean;
  closure_authority?: string;
  risk_triggers?: string[];
  best_practice_skills?: string[];
  prompt_preset?: string;
  prompt?: string;
  catalog_id?: string;
  catalog_version?: string;
  catalog_sha256?: string;
  inline_overrides?: Record<string, unknown>;
  enabled?: boolean;
};

export type ReviewCatalogEntry = {
  id: string;
  version?: string;
  kind?: "agent" | "contract";
  catalog_id?: string;
  catalog_sha256?: string;
  label?: string;
  description?: string;
  default_agent?: string;
  default_model?: string;
  default_preset?: string;
  review_level?: string;
  proof_level?: string;
  reducer_policy?: string;
  read_only?: boolean;
  evidence_required?: boolean;
  closure_authority?: string;
  risk_triggers?: string[];
  best_practice_skills?: string[];
  compatible_node_types?: string[];
  compatible_upstream_types?: string[];
  compatible_downstream_types?: string[];
  required_fields?: string[];
  default?: boolean;
  order?: number;
  prompt?: string;
  source_path?: string;
};

export type ReviewCatalog = {
  schema_version?: string;
  skill?: string;
  source_root?: string;
  agents?: ReviewCatalogEntry[];
  contracts?: ReviewCatalogEntry[];
  default_contracts?: string[];
};

export type ExecGraph = {
  exec_graph_version: string;
  graph_id: string;
  graph_goal: string;
  self_improvement_iterations?: number;
  review_fanout_limits?: ReviewDomainLimits;
  review_iteration_limits?: ReviewDomainLimits;
  nodes: ExecGraphNode[];
};

export type ReviewDomainLimits = {
  review_code?: number;
  review_design?: number;
  review_prompt?: number;
};

export type ExecStatus = {
  state?: string;
  updated_at?: string;
  node_results?: Record<string, Record<string, unknown>>;
  paused?: boolean;
  paused_graph?: boolean;
  paused_node_ids?: string[];
  disabled_node_ids?: string[];
  running_node_ids?: string[];
  runtime_actions?: RuntimeActionRecord[];
};

export type RuntimeActionRecord = {
  schema_version?: string;
  action_id?: string;
  run_id?: string;
  action?: string;
  target?: "graph" | "node" | "subtree";
  node_id?: string | null;
  affected_node_ids?: string[];
  actor?: string;
  reason?: string | null;
  provenance?: Record<string, unknown>;
  status?: string;
  created_at?: string;
};

export type RuntimeActionRequest = {
  action: "pause" | "resume" | "disable" | "cancel" | "stop";
  target: "graph" | "node" | "subtree";
  node_id?: string;
  actor?: string;
  reason?: string;
  provenance?: Record<string, unknown>;
};

type RuntimeActionHandler = (action: RuntimeActionRequest) => unknown | Promise<unknown>;

export type ExecEvent = {
  ts?: string;
  type: string;
  event_type?: string;
  node_id?: string;
  text?: string;
  actor?: string;
  state?: ExecNodeState;
};

export type ExecGraphDebuggerConnection = {
  state: "live" | "loading" | "error" | "static";
  label: string;
  updated_at?: string;
  error?: string;
};

type LayoutNode = ExecGraphNode & {
  x: number;
  y: number;
  depth: number;
  state: ExecNodeState;
};

type LayoutEdge = {
  id: string;
  source: LayoutNode;
  target: LayoutNode;
  path: string;
};

type DebuggerMode = "evidence" | "plan_edit" | "nico_proposals";
type EventFilter = "all" | ExecNodeState;
type AmendPlanContext = {
  baseGraph: ExecGraph;
  diff: PlanDiffItem[];
  validation: PlanValidationResult;
  warning_acceptance?: {
    accepted: boolean;
    actor: string;
    accepted_at: string;
    warnings: PlanValidationIssue[];
  };
};
type AmendPlanHandler = (graph: ExecGraph, context: AmendPlanContext) => unknown | Promise<unknown>;
type ExecGraphAmendmentStatus = "proposed" | "approved" | "rejected" | "superseded";
type ExecGraphAmendment = {
  _key: string;
  graph_id: string;
  run_id?: string;
  base_graph_sha256?: string;
  draft_graph_sha256?: string;
  base_graph_hash?: string;
  baseGraphHash?: string;
  status: ExecGraphAmendmentStatus;
  apply_status?: "applied";
  applied_by?: string;
  applied_at?: string;
  applied_graph_sha256?: string;
  apply_reason?: string;
  actor?: string;
  status_actor?: string;
  status_reason?: string;
  created_at?: string;
  updated_at?: string;
  base_graph?: ExecGraph;
  draft_graph?: ExecGraph;
  diff?: PlanDiffItem[];
};
type AmendmentsLoadState =
  | { status: "idle" | "loading" | "loaded"; message?: string }
  | { status: "error"; message: string };
type AmendmentStatusHandler = (amendmentKey: string, status: Exclude<ExecGraphAmendmentStatus, "proposed">, reason?: string) => unknown | Promise<unknown>;
type AmendmentApplyHandler = (amendment: ExecGraphAmendment, reason?: string) => unknown | Promise<unknown>;
type SaveReviewCatalogEntryHandler = (kind: "agents" | "contracts", entry: ReviewCatalogEntry) => unknown | Promise<unknown>;
type PlanAuditEntry = {
  id: string;
  ts: string;
  actor: string;
  action: string;
  details: string;
  diffRefs?: string[];
  before?: unknown;
  after?: unknown;
};

const nodeWidth = 220;
const nodeHeight = 128;

const fallbackReviewCodeContracts: ReviewCatalogEntry[] = [
  { id: "correctness_regression", label: "Correctness / Regression", default_agent: "correctness-reviewer", default_model: "oc-kimi", default_preset: "scope_default", review_level: "default", proof_level: "static_confirmed", reducer_policy: "evidence_backed_only", read_only: true, evidence_required: true, closure_authority: "final_review_gate", best_practice_skills: ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-python", "best-practices-d3"], default: true },
  { id: "tests_validation", label: "Tests / Validation", default_agent: "validation-reviewer", default_model: "oc-deepseek", default_preset: "scope_default", review_level: "default", proof_level: "proven", reducer_policy: "evidence_backed_only", read_only: true, evidence_required: true, closure_authority: "final_review_gate", best_practice_skills: ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-python", "best-practices-d3"], default: true },
  { id: "simplicity_maintainability", label: "Simplicity / Maintainability", default_agent: "maintainability-reviewer", default_model: "oc-glm", default_preset: "scope_default", review_level: "default", proof_level: "static_confirmed", reducer_policy: "evidence_backed_only", read_only: true, evidence_required: true, closure_authority: "final_review_gate", best_practice_skills: ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-python", "best-practices-d3"], default: true },
  { id: "evidence_closure_safety", label: "Evidence / Closure Safety", default_agent: "scillm-evidence-reviewer", default_model: "gpt-5.5", default_preset: "scope_default", review_level: "default", proof_level: "proven", reducer_policy: "fail_closed_evidence_closure", read_only: true, evidence_required: true, closure_authority: "final_review_gate", best_practice_skills: ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-d3"], risk_triggers: ["evidence", "phase_closure", "artifacts", "orchestration"] },
  { id: "security", label: "Security", default_agent: "security-reviewer", default_model: "gpt-5.5", default_preset: "scope_default", review_level: "risk_expanded", proof_level: "static_confirmed", reducer_policy: "evidence_backed_only", read_only: true, evidence_required: true, closure_authority: "final_review_gate", best_practice_skills: ["best-practices-security", "best-practices-scillm"], risk_triggers: ["auth", "permissions", "secrets", "shell", "file_io", "network", "deserialization", "tokens"] },
];

const fallbackReviewCodeAgents: ReviewCatalogEntry[] = [
  { id: "correctness-reviewer", label: "Correctness Reviewer", default_model: "oc-kimi" },
  { id: "validation-reviewer", label: "Validation Reviewer", default_model: "oc-deepseek" },
  { id: "maintainability-reviewer", label: "Maintainability Reviewer", default_model: "oc-glm" },
  { id: "scillm-evidence-reviewer", label: "scillm Evidence Reviewer", default_model: "gpt-5.5" },
  { id: "security-reviewer", label: "Security Reviewer", default_model: "gpt-5.5" },
];

const reviewCodeContractFallbackIds = [
  "correctness_regression",
  "tests_validation",
  "simplicity_maintainability",
  "evidence_closure_safety",
  "security",
];

const reviewCodeModelOptions = [
  "gpt-5.5",
  "oc-kimi",
  "oc-glm",
  "oc-deepseek",
  "oc-qwen",
];

function isDeprecatedReviewModel(model?: string): boolean {
  const value = String(model ?? "").trim();
  return value === "text" || value.startsWith("text-") || value === "local-text" || value === "moonshot-text";
}

const reviewLevelOptions = [
  { id: "default", label: "Default" },
  { id: "risk_expanded", label: "Risk expanded" },
  { id: "adversarial", label: "Adversarial" },
  { id: "proof_gapfill", label: "Proof gapfill" },
];

const proofLevelOptions = [
  { id: "proven", label: "Proven" },
  { id: "static_confirmed", label: "Static-confirmed" },
  { id: "likely", label: "Likely" },
  { id: "speculative", label: "Speculative" },
];

const reviewCodePromptPresetOptions = [
  { id: "scope_default", label: "Contract default" },
  { id: "prior_round_followup", label: "Prior round follow-up" },
  { id: "strict_blocker_hunt", label: "Strict blocker hunt" },
  { id: "custom", label: "Custom" },
];

const stateLabel: Record<ExecNodeState, string> = {
  pending: "Pending",
  ready: "Ready",
  queued: "Queued",
  running: "Running",
  paused: "Paused",
  passed: "Passed",
  needs_attention: "Needs attention",
  failed: "Failed",
  skipped: "Skipped",
  stopped: "Stopped",
};

const stateColor: Record<ExecNodeState, string> = {
  pending: "var(--exec-pending, #64748b)",
  ready: "var(--exec-ready, #3b82f6)",
  queued: "var(--exec-ready, #3b82f6)",
  running: "var(--exec-running, #f59e0b)",
  paused: "var(--exec-paused, #a855f7)",
  passed: "var(--exec-passed, #22c55e)",
  needs_attention: "var(--exec-needs, #fb923c)",
  failed: "var(--exec-failed, #ef4444)",
  skipped: "var(--exec-skipped, #475569)",
  stopped: "var(--exec-stopped, #111827)",
};

const dimColor = "var(--exec-dim-contrast, #b8c2d6)";

function useRegisterAction(_qid: string, _details: Record<string, unknown>) {
  // Replace with ux-lab's real useRegisterAction hook when integrated.
}

function useSize() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 900, height: 620 });

  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect) return;
      setSize({ width: Math.max(360, rect.width), height: Math.max(360, rect.height) });
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  return { ref, size };
}

function resultState(result: Record<string, unknown> | undefined): ExecNodeState {
  if (!result) return "pending";
  const status = String(result.status ?? "").toLowerCase();
  const failure = String(result.failure_type ?? "").toLowerCase();
  if (status === "skipped" || failure === "dependency_failed") return "skipped";
  if (status === "cancelled" || failure === "cancelled") return "stopped";
  if (result.ok === true) return "passed";
  if (result.ok === false) return "failed";
  return "pending";
}

function nodeResult(status: ExecStatus | undefined, nodeId: string) {
  return status?.node_results?.[nodeId];
}

function initialSelectedNodeId(graph: ExecGraph) {
  return graph.nodes.find((node) => node.id.includes("review-code"))?.id ?? graph.nodes[0]?.id ?? "";
}

function isOptionalNode(node: ExecGraphNode, result?: Record<string, unknown>) {
  if (result?.optional === true || result?.required === false) return true;
  return node.id.includes("optional") || node.type.includes("optional");
}

function requiredEvidenceDefect(node: ExecGraphNode, result: Record<string, unknown> | undefined, state: ExecNodeState) {
  if (!result || isOptionalNode(node, result) || node.type === "claude_print") return false;
  if (state !== "passed") return false;
  const evidenceStatusText = String(result.evidence_status ?? "").toLowerCase();
  const hasHash = Boolean(result.output_hash);
  const hasArtifact = Boolean(result.output_artifact ?? result.artifact ?? result.artifacts);
  if (!hasHash || !hasArtifact) return true;
  return Boolean(evidenceStatusText && evidenceStatusText !== "hash_bound" && evidenceStatusText !== "evidence hash reported");
}

function runSummary(graph: ExecGraph, status: ExecStatus | undefined, states: Record<string, ExecNodeState>) {
  const lifecycle = String(status?.state ?? "unknown");
  let passed = 0;
  let failed = 0;
  let optionalFailed = 0;
  let requiredFailed = 0;
  let running = 0;
  let pending = 0;
  const requiredEvidenceDefectNodes: string[] = [];

  for (const node of graph.nodes) {
    const state = states[node.id] ?? "pending";
    const result = nodeResult(status, node.id);
    if (state === "passed") passed += 1;
    if (state === "failed") {
      failed += 1;
      if (isOptionalNode(node, result)) optionalFailed += 1;
      else requiredFailed += 1;
    }
    if (state === "running") running += 1;
    if (state === "pending" || state === "ready" || state === "queued") pending += 1;
    if (requiredEvidenceDefect(node, result, state)) requiredEvidenceDefectNodes.push(node.id);
  }

  const result =
    requiredFailed > 0
      ? `Failed · ${requiredFailed} required failed`
      : requiredEvidenceDefectNodes.length > 0
        ? `Blocked · ${requiredEvidenceDefectNodes.length} evidence defect${requiredEvidenceDefectNodes.length === 1 ? "" : "s"}`
      : optionalFailed > 0
        ? lifecycle === "completed"
          ? `Passed with ${optionalFailed} optional failure${optionalFailed === 1 ? "" : "s"}`
          : `Required clear so far · ${optionalFailed} optional failure${optionalFailed === 1 ? "" : "s"}`
        : lifecycle === "completed"
          ? "Passed"
          : running > 0
            ? "Running"
            : "Pending";

  return { lifecycle, result, passed, failed, optionalFailed, requiredFailed, running, pending, requiredEvidenceDefects: requiredEvidenceDefectNodes.length, requiredEvidenceDefectNodes };
}

function buildStates(graph: ExecGraph, status?: ExecStatus, events: ExecEvent[] = []): Record<string, ExecNodeState> {
  const states: Record<string, ExecNodeState> = {};
  for (const node of graph.nodes) states[node.id] = "pending";

  for (const event of events) {
    if (!event.node_id) continue;
    const semanticType = event.event_type ?? event.type;
    if (semanticType === "node_scheduled") states[event.node_id] = "queued";
    if (semanticType === "node_started" || semanticType === "subagent_started" || semanticType === "transcript_delta" || semanticType === "project_agent_input_sent") states[event.node_id] = "running";
    if (semanticType === "breakpoint_hit") states[event.node_id] = "paused";
    if (semanticType === "node_finished" || semanticType === "subagent_final") states[event.node_id] = event.type.includes("failed") ? "failed" : "passed";
    if (semanticType === "node_failed") states[event.node_id] = "failed";
    if (semanticType === "node_skipped") states[event.node_id] = "skipped";
    if (semanticType === "needs_attention" || semanticType === "human_input_requested") states[event.node_id] = "needs_attention";
  }

  for (const node of graph.nodes) {
    const terminalState = resultState(status?.node_results?.[node.id]);
    if (terminalState !== "pending") states[node.id] = terminalState;
  }

  for (const node of graph.nodes) {
    if (states[node.id] !== "pending") continue;
    const deps = node.depends_on ?? [];
    if (deps.length === 0 || deps.every((dep) => states[dep] === "passed")) states[node.id] = "ready";
  }
  return states;
}

function layout(graph: ExecGraph, states: Record<string, ExecNodeState>, width: number, height: number, selectedId?: string) {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const selectedNode = selectedId ? byId.get(selectedId) : undefined;
  const selectedNeighborhood = new Set<string>();
  if (selectedNode) {
    selectedNeighborhood.add(selectedNode.id);
    for (const dependency of selectedNode.depends_on ?? []) selectedNeighborhood.add(dependency);
    for (const node of graph.nodes) {
      if ((node.depends_on ?? []).includes(selectedNode.id)) selectedNeighborhood.add(node.id);
    }
  }
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const computeDepth = (id: string): number => {
    if (depth.has(id)) return depth.get(id)!;
    if (visiting.has(id)) return 0;
    const node = byId.get(id);
    const deps = (node?.depends_on ?? []).filter((dependency) => byId.has(dependency));
    visiting.add(id);
    const value = deps.length === 0 ? 0 : Math.max(...deps.map(computeDepth)) + 1;
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };

  for (const node of graph.nodes) computeDepth(node.id);
  const layers = d3.group(graph.nodes, (node) => depth.get(node.id) ?? 0);
  const horizontalLayerGap = Math.max(nodeWidth + 120, Math.min(nodeWidth + 180, width / 3));
  const xForLayer = (layer: number) => nodeWidth / 2 + 90 + layer * horizontalLayerGap;
  const nodes: LayoutNode[] = [];

  for (const [layer, layerNodes] of layers) {
    const prioritizedLayerNodes = [...layerNodes].sort((a, b) => {
      const aPriority = a.id === selectedId ? 0 : selectedNeighborhood.has(a.id) ? 1 : 2;
      const bPriority = b.id === selectedId ? 0 : selectedNeighborhood.has(b.id) ? 1 : 2;
      if (aPriority !== bPriority) return aPriority - bPriority;
      return a.id.localeCompare(b.id);
    });
    const verticalPadding = nodeHeight / 2 + 32;
    const yScale = d3.scalePoint<string>().domain(prioritizedLayerNodes.map((node) => node.id)).range([verticalPadding, Math.max(verticalPadding + 48, height - verticalPadding)]).padding(0.8);
    const layerHasSelectedNeighborhood = prioritizedLayerNodes.some((node) => selectedNeighborhood.has(node.id));
    for (const [index, node] of prioritizedLayerNodes.entries()) {
      const compactY = verticalPadding + 150 + index * (nodeHeight + 36);
      nodes.push({
        ...node,
        depth: layer,
        state: states[node.id] ?? "pending",
        x: xForLayer(layer),
        y: layerHasSelectedNeighborhood ? compactY : yScale(node.id) ?? height / 2,
      });
    }
  }

  const layoutById = new Map(nodes.map((node) => [node.id, node]));
  const line = d3.line<[number, number]>().curve(d3.curveBumpX);
  const edges: LayoutEdge[] = [];
  for (const target of nodes) {
    for (const dep of target.depends_on ?? []) {
      const source = layoutById.get(dep);
      if (!source) continue;
      const mid = (source.x + target.x) / 2;
      edges.push({
        id: `${source.id}->${target.id}`,
        source,
        target,
        path: line([[source.x + nodeWidth / 2, source.y], [mid, source.y], [mid, target.y], [target.x - nodeWidth / 2, target.y]]) ?? "",
      });
    }
  }
  return { nodes, edges };
}

function formatTimestamp(value?: string) {
  if (!value) return "no timestamp";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.toISOString().replace(/\.\d{3}Z$/, "").replace("T", " ")} UTC`;
}

function jsonHeaders(headers?: HeadersInit): HeadersInit {
  const merged = new Headers(headers);
  merged.set("Content-Type", "application/json");
  return merged;
}

function formatEventTime(value?: string) {
  if (!value) return "no time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().slice(11, 19);
}

function formatEvidenceValue(label: string, value: unknown) {
  if (value === undefined || value === null || value === "") return value;
  const lowerLabel = label.toLowerCase();
  if (lowerLabel.includes("started") || lowerLabel.includes("completed") || lowerLabel.includes("time")) {
    return formatTimestamp(String(value));
  }
  return value;
}

function titleCase(value: string) {
  return value ? value.slice(0, 1).toUpperCase() + value.slice(1).replaceAll("_", " ") : "Unknown";
}

function optionLabel(option: { label: string; description?: string } | string) {
  return typeof option === "string" ? option : option.label;
}

function optionDescription(option: { label: string; description?: string } | string) {
  return typeof option === "string" ? "" : option.description ?? "";
}

function isReviewCodeNode(node: ExecGraphNode) {
  return /review-code|review_code/i.test(`${node.type} ${node.protocol_role ?? ""} ${node.node_goal}`);
}

function reviewContractName(scope: ReviewScopeSpec) {
  return String(scope.contract ?? scope.scope ?? "").trim();
}

function reviewCatalogAgents(catalog?: ReviewCatalog) {
  return catalog?.agents?.length ? catalog.agents : fallbackReviewCodeAgents;
}

function reviewCatalogContracts(catalog?: ReviewCatalog) {
  return catalog?.contracts?.length ? catalog.contracts : fallbackReviewCodeContracts;
}

function reviewCatalogDefaultContracts(catalog?: ReviewCatalog) {
  const contracts = reviewCatalogContracts(catalog);
  const catalogDefaults = catalog?.default_contracts?.filter(Boolean);
  if (catalogDefaults?.length) return catalogDefaults;
  const frontmatterDefaults = contracts.filter((contract) => contract.default).map((contract) => contract.id);
  return frontmatterDefaults.length ? frontmatterDefaults : reviewCodeContractFallbackIds.slice(0, 3);
}

function contractRiskTriggered(contract: string, node: ExecGraphNode, catalog?: ReviewCatalog, allNodes: ExecGraphNode[] = []) {
  const entry = reviewContractEntry(contract, catalog);
  const haystack = JSON.stringify({
    node,
    adjacent: allNodes.filter((candidate) => candidate.id === node.id || (node.depends_on ?? []).includes(candidate.id)),
  }).toLowerCase();
  if (contract === "evidence_closure_safety") {
    return /scillm|plan-iterate|phase|closure|evidence|artifact|provenance|review|orchestration|execution_result|hash/.test(haystack);
  }
  if (contract === "security") {
    return /auth|permission|secret|shell|command|file|network|deserialize|token|credential|oauth|path/.test(haystack);
  }
  const triggers = entry?.risk_triggers ?? [];
  return triggers.some((trigger) => haystack.includes(trigger.toLowerCase()));
}

function reviewCatalogDefaultContractsForNode(node: ExecGraphNode, catalog?: ReviewCatalog, allNodes: ExecGraphNode[] = []) {
  const defaults = reviewCatalogDefaultContracts(catalog);
  const contracts = reviewCatalogContracts(catalog);
  const triggered = contracts.filter((contract) => !defaults.includes(contract.id) && contractRiskTriggered(contract.id, node, catalog, allNodes)).map((contract) => contract.id);
  return [...defaults, ...triggered];
}

function _mergeReviewCatalogEntry(catalog: ReviewCatalog | undefined, kind: "agents" | "contracts", entry: ReviewCatalogEntry): ReviewCatalog {
  const base: ReviewCatalog = catalog ?? { schema_version: "scillm.exec.review_catalog.v1", skill: "review-code" };
  const current = kind === "agents" ? reviewCatalogAgents(base) : reviewCatalogContracts(base);
  const merged = [...current.filter((candidate) => candidate.id !== entry.id), entry].sort((a, b) => (a.order ?? 9999) - (b.order ?? 9999) || a.id.localeCompare(b.id));
  return kind === "agents" ? { ...base, agents: merged } : { ...base, contracts: merged };
}

function reviewContractEntry(contract: string, catalog?: ReviewCatalog) {
  return reviewCatalogContracts(catalog).find((entry) => entry.id === contract);
}

function defaultReviewAgentForContract(contract: string, catalog?: ReviewCatalog) {
  return reviewContractEntry(contract, catalog)?.default_agent ?? "correctness-reviewer";
}

function defaultReviewModelForContract(contract: string, catalog?: ReviewCatalog) {
  const contractEntry = reviewContractEntry(contract, catalog);
  const agentEntry = reviewCatalogAgents(catalog).find((entry) => entry.id === contractEntry?.default_agent);
  return contractEntry?.default_model ?? agentEntry?.default_model ?? "oc-kimi";
}

function defaultBestPracticeSkillsForContract(contract: string, catalog?: ReviewCatalog) {
  const entry = reviewContractEntry(contract, catalog);
  if (entry?.best_practice_skills?.length) return entry.best_practice_skills;
  if (contract === "security") return ["best-practices-security", "best-practices-scillm"];
  if (contract === "evidence_closure_safety") return ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-d3"];
  return ["best-practices-scillm", "best-practices-self-improvement-loop", "best-practices-python", "best-practices-d3"];
}

function catalogIdentityFields(entry?: ReviewCatalogEntry): Pick<ReviewScopeSpec, "catalog_id" | "catalog_version" | "catalog_sha256"> {
  return {
    catalog_id: entry?.catalog_id,
    catalog_version: entry?.version,
    catalog_sha256: entry?.catalog_sha256,
  };
}

function formatBestPracticeSkills(skills?: string[]) {
  return (skills ?? []).join(", ");
}

function parseBestPracticeSkills(value: string) {
  const seen = new Set<string>();
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter((item) => {
      if (!item || seen.has(item)) return false;
      seen.add(item);
      return true;
    });
}

function defaultReviewScopeForContract(contract: string, node: ExecGraphNode, catalog?: ReviewCatalog): ReviewScopeSpec {
  const entry = reviewContractEntry(contract, catalog);
  return {
    scope: contract,
    contract,
    ...catalogIdentityFields(entry),
    agent: defaultReviewAgentForContract(contract, catalog),
    model: node.model || defaultReviewModelForContract(contract, catalog),
    review_level: entry?.review_level ?? (contract === "security" ? "risk_expanded" : "default"),
    proof_level: entry?.proof_level ?? (contract === "tests_validation" || contract === "evidence_closure_safety" ? "proven" : "static_confirmed"),
    reducer_policy: entry?.reducer_policy ?? (contract === "evidence_closure_safety" ? "fail_closed_evidence_closure" : "evidence_backed_only"),
    read_only: entry?.read_only ?? true,
    evidence_required: entry?.evidence_required ?? true,
    closure_authority: entry?.closure_authority ?? "final_review_gate",
    risk_triggers: entry?.risk_triggers,
    best_practice_skills: defaultBestPracticeSkillsForContract(contract, catalog),
    prompt_preset: entry?.default_preset ?? "scope_default",
    prompt: defaultReviewContractPrompt(contract, entry?.default_preset ?? "scope_default", catalog),
    inline_overrides: {},
    enabled: true,
  };
}

function defaultReviewContractPrompt(contract: string, preset = "scope_default", catalog?: ReviewCatalog) {
  const catalogPrompt = reviewContractEntry(contract, catalog)?.prompt?.trim();
  if (catalogPrompt) {
    if (preset === "prior_round_followup") {
      return `${catalogPrompt} Check the prior-round adjudication table first: implemented findings, deferred accepted findings, and rejected reviewer claims with rationale. Do not repeat rejected unsupported claims unless new evidence contradicts the rejection.`;
    }
    if (preset === "strict_blocker_hunt") {
      return `${catalogPrompt} Report only concrete merge-blocking findings with file/diff/test/log/artifact evidence. Put unsupported concerns in unsupported_or_rejected_concerns.`;
    }
    return catalogPrompt;
  }
  const contractPrompts: Record<string, string> = {
    correctness_regression: "Determine whether the diff satisfies the requested change without breaking existing behavior. Return strict JSON using the review-code scoped evidence schema.",
    tests_validation: "Determine whether validation is sufficient for the risk introduced by the diff. Return strict JSON using the review-code scoped evidence schema.",
    simplicity_maintainability: "Identify concrete unnecessary complexity introduced by this diff. Return strict JSON using the review-code scoped evidence schema.",
    evidence_closure_safety: "Check scillm evidence, provenance, artifact, review, and phase-closure invariants. Return strict JSON using the review-code scoped evidence schema.",
    security: "Review auth, permissions, secrets, shell commands, file IO, network IO, deserialization, user input, path handling, tokens, and sensitive logs. Return strict JSON using the review-code scoped evidence schema.",
  };
  if (preset === "prior_round_followup") {
    return `${contractPrompts[contract] ?? "Run this evidence contract."} Check the prior-round adjudication table first: implemented findings, deferred accepted findings, and rejected reviewer claims with rationale. Do not repeat rejected unsupported claims unless new evidence contradicts the rejection.`;
  }
  if (preset === "strict_blocker_hunt") {
    return `${contractPrompts[contract] ?? "Run this evidence contract."} Report only concrete merge-blocking findings with file/diff/test/log/artifact evidence. Put unsupported concerns in unsupported_or_rejected_concerns.`;
  }
  return contractPrompts[contract] ?? "Run this evidence contract and return strict JSON using the review-code scoped evidence schema.";
}

function modelChoices(availableModels?: string[], currentModel?: string) {
  const values = new Set(["", ...reviewCodeModelOptions, ...(availableModels ?? [])]);
  if (currentModel) values.add(currentModel);
  return Array.from(values);
}

function reviewScopeModelChoices(availableModels?: string[], scopes: ReviewScopeSpec[] = []) {
  const values = new Set(["", ...reviewCodeModelOptions, ...(availableModels ?? []).filter((model) => !isDeprecatedReviewModel(model))]);
  for (const scope of scopes) {
    if (scope.model) values.add(scope.model);
  }
  return Array.from(values);
}

function eventTone(event: ExecEvent): ExecNodeState {
  const semanticType = event.event_type ?? event.type;
  if (event.type.includes("failed") || event.state === "failed") return "failed";
  if (semanticType.includes("needs_attention") || semanticType === "human_input_requested" || event.state === "needs_attention") return "needs_attention";
  if (semanticType.includes("finished") || semanticType === "subagent_final" || event.state === "passed") return "passed";
  if (semanticType.includes("started") || semanticType === "project_agent_input_sent" || semanticType === "transcript_delta" || event.state === "running") return "running";
  if (semanticType.includes("skipped") || event.state === "skipped") return "skipped";
  if (semanticType.includes("paused") || event.state === "paused") return "paused";
  if (semanticType.includes("stopped") || event.state === "stopped") return "stopped";
  return "pending";
}

function nodeById(graph: ExecGraph, nodeId: string) {
  return graph.nodes.find((node) => node.id === nodeId);
}

function patchAuditEntry(actor: string, patch: Parameters<typeof applyPlanPatch>[1], beforeGraph: ExecGraph, afterGraph: ExecGraph): PlanAuditEntry {
  const beforeNode = "node_id" in patch ? nodeById(beforeGraph, patch.node_id) : undefined;
  const afterNode = "node_id" in patch ? nodeById(afterGraph, patch.node_id) : patch.op === "add_node" ? nodeById(afterGraph, patch.node.id) : undefined;
  const ts = new Date().toISOString();

  if (patch.op === "update_node") {
    const fields = Object.keys(patch.fields).join(", ");
    return {
      id: `${ts}-${patch.op}-${patch.node_id}`,
      ts,
      actor,
      action: "node updated",
      details: `${patch.node_id}: ${fields}`,
      before: beforeNode,
      after: afterNode,
    };
  }

  if (patch.op === "add_dependency" || patch.op === "remove_dependency") {
    return {
      id: `${ts}-${patch.op}-${patch.node_id}-${patch.depends_on}`,
      ts,
      actor,
      action: patch.op === "add_dependency" ? "dependency added" : "dependency removed",
      details: `${patch.depends_on} -> ${patch.node_id}`,
      before: beforeNode?.depends_on ?? [],
      after: afterNode?.depends_on ?? [],
    };
  }

  if (patch.op === "add_node") {
    return {
      id: `${ts}-${patch.op}-${patch.node.id}`,
      ts,
      actor,
      action: "node added",
      details: patch.node.id,
      after: nodeById(afterGraph, patch.node.id),
    };
  }

  return {
    id: `${ts}-${patch.op}-${patch.node_id}`,
    ts,
    actor,
    action: "node removed",
    details: patch.node_id,
    before: beforeNode,
  };
}

function diffRefsForChange(beforeGraph: ExecGraph, afterGraph: ExecGraph): string[] {
  return diffExecGraphPlan(beforeGraph, afterGraph).map((_, index) => `Diff ${index + 1}`);
}

function resetAuditEntry(beforeGraph: ExecGraph, afterGraph: ExecGraph): PlanAuditEntry {
  const ts = new Date().toISOString();
  return {
    id: `${ts}-reset-draft`,
    ts,
    actor: "local editor",
    action: "draft reset",
    details: "Draft returned to immutable execution graph.",
    before: beforeGraph,
    after: afterGraph,
  };
}

function rejectedPatchAuditEntry(actor: string, patch: Parameters<typeof applyPlanPatch>[1], graph: ExecGraph, issue: PlanValidationIssue, selectedNodeId: string): PlanAuditEntry {
  const ts = new Date().toISOString();
  const attemptedOperation = patchSummary(patch);
  return {
    id: `${ts}-rejected-${attemptedOperation.replaceAll(" ", "-")}`,
    ts,
    actor,
    action: "rejected patch attempt",
    details: `${attemptedOperation} · mutated_draft: false · ${issue.message}`,
    before: {
      selected_node_id: selectedNodeId,
      attempted_operation: patch,
      validation_result: issue,
      mutated_draft: false,
    },
    after: graph,
  };
}

type AmendState =
  | { status: "idle" }
  | { status: "saving"; message: string }
  | { status: "saved"; message: string; amendment_key?: string; local_amendment_id: string; diff_hash: string; saved_at: string; graph_id: string; diff_count: number; acknowledged_warning_ids: string[]; proposal_ids: string[] }
  | { status: "error"; message: string };

function formatJsonBlock(value: unknown) {
  return JSON.stringify(value ?? null, null, 2);
}

function patchSummary(patch: Parameters<typeof applyPlanPatch>[1]) {
  if (patch.op === "update_node") return `update_node ${patch.node_id}.${Object.keys(patch.fields).join(",")}`;
  if (patch.op === "add_node") return `add_node ${patch.node.id}`;
  if (patch.op === "remove_node") return `remove_node ${patch.node_id}`;
  if (patch.op === "add_dependency") return `add_dependency ${patch.depends_on} -> ${patch.node_id}`;
  return `remove_dependency ${patch.depends_on} -> ${patch.node_id}`;
}

function diffNodeIds(item: PlanDiffItem): string[] {
  const ids = new Set<string>([item.node_id]);
  if (item.dependency) ids.add(item.dependency);
  return Array.from(ids);
}

function diffParticipationSummary(item: PlanDiffItem, nodeId: string) {
  if (item.kind === "dependency_added" && item.node_id === nodeId) return `dependency added from ${String(item.dependency)}`;
  if (item.kind === "dependency_added" && item.dependency === nodeId) return `added dependency to ${item.node_id}`;
  if (item.kind === "dependency_removed" && item.node_id === nodeId) return `dependency removed: ${String(item.dependency)}`;
  if (item.kind === "dependency_removed" && item.dependency === nodeId) return `removed dependency from ${item.node_id}`;
  if (item.kind === "node_updated") return `updated ${String(item.field ?? "node")}`;
  if (item.kind === "node_added") return "added node";
  if (item.kind === "node_removed") return "removed node";
  return item.label;
}

function warningIdentity(issue: PlanValidationIssue) {
  return [issue.code, issue.node_id ?? "graph", issue.path, issue.contract].filter(Boolean).join(":");
}

function localIdentity(value: unknown) {
  const text = JSON.stringify(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `local-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function cyclePathForDependency(nodeId: string, dependencyId: string, allNodes: ExecGraphNode[]) {
  const byId = new Map(allNodes.map((item) => [item.id, item]));
  const visit = (currentId: string, path: string[]): string[] | undefined => {
    if (currentId === nodeId) return [...path, currentId];
    const current = byId.get(currentId);
    for (const next of current?.depends_on ?? []) {
      if (next === nodeId) return [...path, currentId, next];
      if (path.includes(next)) continue;
      const result = visit(next, [...path, currentId]);
      if (result) return result;
    }
    return undefined;
  };
  const path = visit(dependencyId, [nodeId]);
  return path ? path.join(" -> ") : "";
}

function dependencyList(value: unknown, addedDependency?: string) {
  if (!Array.isArray(value)) return value ? [String(value)] : [];
  return value.map((item) => String(item)).map((item) => item === addedDependency ? `+ ${item}` : item);
}

function runImpact(summary: ReturnType<typeof runSummary>) {
  if (summary.requiredFailed > 0) return `Blocking required nodes failed: ${summary.requiredFailed}`;
  if (summary.requiredEvidenceDefects > 0) return `Closure blocked: ${summary.requiredEvidenceDefects} required node${summary.requiredEvidenceDefects === 1 ? "" : "s"} missing hash/artifact evidence.`;
  if (summary.optionalFailed > 0 && summary.lifecycle === "completed") return "Run passed because 0 required nodes failed.";
  if (summary.optionalFailed > 0) return "No required failures yet; optional failure is non-blocking.";
  if (summary.lifecycle === "completed") return "Run passed because all required nodes passed.";
  return "Run result is still pending.";
}

function currentVerdict(summary: ReturnType<typeof runSummary>, terminal: boolean) {
  if (summary.requiredEvidenceDefects > 0) return { label: "Evidence gate", text: `Blocked - ${summary.requiredEvidenceDefects} required evidence defect${summary.requiredEvidenceDefects === 1 ? "" : "s"}` };
  if (terminal) return { label: "Final result", text: summary.result };
  if (summary.requiredFailed > 0) return { label: "Current verdict", text: `Blocked — ${summary.requiredFailed} required failure${summary.requiredFailed === 1 ? "" : "s"}` };
  if (summary.optionalFailed > 0) return { label: "Current verdict", text: `Required clear so far — ${summary.optionalFailed} optional failure${summary.optionalFailed === 1 ? "" : "s"}` };
  if (summary.running > 0) return { label: "Current verdict", text: "Running" };
  return { label: "Current verdict", text: summary.result };
}

function nodeImpact(optional: boolean, state: ExecNodeState) {
  if (optional && state === "failed") return "Non-blocking because REQUIRED = no";
  if (state === "failed") return "Blocking required failure";
  if (state === "passed") return "Satisfied required evidence";
  return "No terminal impact yet";
}

function nodeImpactText(optional: boolean, state: ExecNodeState, evidenceIncomplete: boolean) {
  if (evidenceIncomplete) return "Execution passed; required evidence incomplete";
  return nodeImpact(optional, state);
}

function evidenceState(label: string, value: unknown, node: ExecGraphNode, optional: boolean) {
  if (value !== undefined && value !== null && value !== "") {
    return { text: String(value), tone: "present", note: "" };
  }
  const lowerLabel = label.toLowerCase();
  if (lowerLabel.includes("hash")) {
    if (optional) return { text: "Allowed optional absence", tone: "optional", note: "Optional evidence not reported." };
    if (node.type === "claude_print") return { text: "Not applicable for claude_print", tone: "na", note: "This runtime does not produce an output hash." };
    return { text: "Missing required output hash", tone: "missing", note: "Required evidence is absent." };
  }
  if (optional) return { text: "Allowed optional absence", tone: "optional", note: "Optional evidence not reported." };
  return { text: "Not reported", tone: "missing", note: "Required evidence is not reported." };
}

function outputHashState(value: unknown, optional: boolean) {
  if (value !== undefined && value !== null && value !== "") {
    return { text: String(value), tone: "present", note: "" };
  }
  if (optional) return { text: "Not reported", tone: "optional", note: "Hash not reported for optional node." };
  return { text: "Missing required output hash", tone: "missing", note: "Required evidence is absent." };
}

function evidenceStatus(result: Record<string, unknown> | undefined, optional: boolean) {
  if (optional && !result?.output_hash) return "Allowed optional absence";
  if (result?.output_hash) return "Evidence hash reported";
  return optional ? "Optional evidence not reported" : "Required evidence incomplete";
}

function artifactLabel(value: unknown) {
  return value ? "Reported artifact" : "Artifact";
}

export function ScillmExecGraphDebugger({
  graph,
  status,
  events = [],
  enablePlanEditing = false,
  nicoProposals = [],
  onAmendPlan,
  onRuntimeAction,
  onApplyAmendment,
  availableModels,
  reviewCatalog,
  runtimeReadiness,
  onSaveReviewCatalogEntry,
}: {
  graph: ExecGraph;
  status?: ExecStatus;
  events?: ExecEvent[];
  enablePlanEditing?: boolean;
  nicoProposals?: NicoPlanProposal[];
  onAmendPlan?: AmendPlanHandler;
  onRuntimeAction?: RuntimeActionHandler;
  onApplyAmendment?: AmendmentApplyHandler;
  availableModels?: string[];
  reviewCatalog?: ReviewCatalog;
  runtimeReadiness?: RuntimeReadinessReport;
  onSaveReviewCatalogEntry?: SaveReviewCatalogEntryHandler;
}) {
  return <ScillmExecGraphDebuggerView graph={graph} status={status} events={events} connection={{ state: "static", label: "Static snapshot" }} enablePlanEditing={enablePlanEditing} nicoProposals={nicoProposals} onAmendPlan={onAmendPlan} onRuntimeAction={onRuntimeAction} onApplyAmendment={onApplyAmendment} availableModels={availableModels} reviewCatalog={reviewCatalog} runtimeReadiness={runtimeReadiness} onSaveReviewCatalogEntry={onSaveReviewCatalogEntry} />;
}

export function ScillmExecGraphDebuggerLive({
  graph,
  runId,
  baseUrl = "",
  headers,
  pollIntervalMs = 2000,
  fetcher = fetch,
  enablePlanEditing = false,
  nicoProposals = [],
  onAmendPlan,
  availableModels: suppliedModels,
  reviewCatalog: suppliedReviewCatalog,
  runtimeReadiness,
}: {
  graph: ExecGraph;
  runId: string;
  baseUrl?: string;
  headers?: HeadersInit;
  pollIntervalMs?: number;
  fetcher?: typeof fetch;
  enablePlanEditing?: boolean;
  nicoProposals?: NicoPlanProposal[];
  onAmendPlan?: AmendPlanHandler;
  availableModels?: string[];
  reviewCatalog?: ReviewCatalog;
  runtimeReadiness?: RuntimeReadinessReport;
}) {
  const [status, setStatus] = useState<ExecStatus | undefined>();
  const [events, setEvents] = useState<ExecEvent[]>([]);
  const [amendments, setAmendments] = useState<ExecGraphAmendment[]>([]);
  const [amendmentsState, setAmendmentsState] = useState<AmendmentsLoadState>({ status: "idle" });
  const [connection, setConnection] = useState<ExecGraphDebuggerConnection>({ state: "loading", label: "Connecting to exec run" });
  const [liveModels, setLiveModels] = useState<string[]>(suppliedModels ?? reviewCodeModelOptions);
  const [liveReviewCatalog, setLiveReviewCatalog] = useState<ReviewCatalog | undefined>(suppliedReviewCatalog);
  const requestSeq = useRef(0);

  async function loadAmendments() {
    setAmendmentsState({ status: "loading", message: "Loading Memory amendments." });
    try {
      const response = await fetcher(`${baseUrl}/v1/scillm/exec/graph/${encodeURIComponent(graph.graph_id)}/amendments?limit=50`, { headers });
      if (!response.ok) throw new Error(`amendments ${response.status}`);
      const payload = await response.json();
      setAmendments(Array.isArray(payload.amendments) ? payload.amendments : []);
      setAmendmentsState({ status: "loaded", message: `${Array.isArray(payload.amendments) ? payload.amendments.length : 0} amendment records loaded.` });
    } catch (error) {
      setAmendmentsState({ status: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const seq = requestSeq.current + 1;
      requestSeq.current = seq;
      try {
        const [statusResponse, eventsResponse] = await Promise.all([
          fetcher(`${baseUrl}/v1/scillm/exec/${encodeURIComponent(runId)}/status`, { headers }),
          fetcher(`${baseUrl}/v1/scillm/exec/${encodeURIComponent(runId)}/events?tail=200`, { headers }),
        ]);
        if (!statusResponse.ok) throw new Error(`status ${statusResponse.status}`);
        if (!eventsResponse.ok) throw new Error(`events ${eventsResponse.status}`);

        const nextStatus = await statusResponse.json();
        const eventPayload = await eventsResponse.json();
        if (cancelled || seq !== requestSeq.current) return;

        setStatus(nextStatus);
        setEvents(Array.isArray(eventPayload.events) ? eventPayload.events : []);
        setConnection({
          state: "live",
          label: `Live exec run · ${String(nextStatus.state ?? "unknown")}`,
          updated_at: String(nextStatus.updated_at ?? new Date().toISOString()),
        });
      } catch (error) {
        if (cancelled || seq !== requestSeq.current) return;
        setConnection({ state: "error", label: "Live exec run unavailable", error: error instanceof Error ? error.message : String(error) });
      }
    }

    void load();
    const interval = window.setInterval(() => void load(), Math.max(500, pollIntervalMs));
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [baseUrl, fetcher, headers, pollIntervalMs, runId]);

  useEffect(() => {
    if (!enablePlanEditing) return;
    void loadAmendments();
  }, [baseUrl, enablePlanEditing, graph.graph_id, headers]);

  useEffect(() => {
    if (suppliedModels?.length) {
      setLiveModels(suppliedModels.filter((model) => !isDeprecatedReviewModel(model)));
      return;
    }
    let cancelled = false;
    async function loadModels() {
      try {
        const response = await fetcher(`${baseUrl}/v1/scillm/models`, { headers });
        if (!response.ok) return;
        const payload = await response.json();
        const registry = payload && typeof payload === "object"
          ? (payload as {
              groups?: Record<string, unknown>;
              models?: Record<string, unknown>;
              aliases?: Record<string, unknown>;
              review_fanout_models?: string[];
              selectable_models?: string[];
            })
          : undefined;
        const endpointModels = registry?.review_fanout_models?.length
          ? registry.review_fanout_models
          : registry?.selectable_models?.length
            ? registry.selectable_models
            : [
                ...Object.keys(registry?.models ?? {}),
                ...Object.keys(registry?.groups ?? {}),
                ...Object.keys(registry?.aliases ?? {}),
              ];
        const modelNames = new Set(endpointModels.filter((model) => !isDeprecatedReviewModel(model)));
        if (!modelNames.size || cancelled) return;
        setLiveModels(Array.from(modelNames).sort());
      } catch {
        if (!cancelled) setLiveModels(reviewCodeModelOptions);
      }
    }
    void loadModels();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, fetcher, headers, suppliedModels]);

  useEffect(() => {
    if (suppliedReviewCatalog) {
      setLiveReviewCatalog(suppliedReviewCatalog);
      return;
    }
    let cancelled = false;
    async function loadReviewCatalog() {
      try {
        const response = await fetcher(`${baseUrl}/v1/scillm/exec/review-catalog?skill=review-code`, { headers });
        if (!response.ok) return;
        const payload = await response.json();
        if (cancelled || !payload || typeof payload !== "object") return;
        setLiveReviewCatalog(payload as ReviewCatalog);
      } catch {
        if (!cancelled) setLiveReviewCatalog(undefined);
      }
    }
    void loadReviewCatalog();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, fetcher, headers, suppliedReviewCatalog]);

  const memoryAmendPlan: AmendPlanHandler = async (draftGraph, context) => {
    const response = await fetcher(`${baseUrl}/v1/scillm/exec/graph/amendments`, {
      method: "POST",
      headers: jsonHeaders(headers),
      body: JSON.stringify({
        graph_id: graph.graph_id,
        run_id: runId,
        base_graph: context.baseGraph,
        draft_graph: draftGraph,
        diff: context.diff,
        validation: context.validation,
        warning_acceptance: context.warning_acceptance,
        actor: "scillm-exec-graph-editor",
        provenance: {
          source: "ScillmExecGraphDebuggerLive",
          run_id: runId,
          status_updated_at: status?.updated_at,
        },
      }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`memory amendment ${response.status}: ${text.slice(0, 240)}`);
    }
    const result = await response.json();
    await loadAmendments();
    return result;
  };

  const memorySetAmendmentStatus: AmendmentStatusHandler = async (amendmentKey, nextStatus, reason) => {
    const response = await fetcher(`${baseUrl}/v1/scillm/exec/graph/amendments/${encodeURIComponent(amendmentKey)}/status`, {
      method: "POST",
      headers: jsonHeaders(headers),
      body: JSON.stringify({
        status: nextStatus,
        actor: "scillm-exec-graph-editor",
        reason,
      }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`amendment status ${response.status}: ${text.slice(0, 240)}`);
    }
    await loadAmendments();
    return response.json();
  };

  const memoryApplyAmendment: AmendmentApplyHandler = async (amendment, reason) => {
    const response = await fetcher(`${baseUrl}/v1/scillm/exec/graph/amendments/${encodeURIComponent(amendment._key)}/apply`, {
      method: "POST",
      headers: jsonHeaders(headers),
      body: JSON.stringify({
        actor: "scillm-exec-graph-editor",
        reason: reason ?? "Applied approved amendment from DAG editor.",
        expected_base_graph_sha256: amendment.base_graph_sha256 ?? amendment.base_graph_hash ?? amendment.baseGraphHash,
        provenance: {
          source: "ScillmExecGraphDebuggerLive",
          graph_id: amendment.graph_id,
          run_id: runId,
          status_updated_at: status?.updated_at,
        },
      }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`amendment apply ${response.status}: ${text.slice(0, 240)}`);
    }
    const result = await response.json();
    await loadAmendments();
    return result;
  };

  const saveReviewCatalogEntry: SaveReviewCatalogEntryHandler = async (kind, entry) => {
    const response = await fetcher(`${baseUrl}/v1/scillm/exec/review-catalog/${kind}?skill=review-code`, {
      method: "POST",
      headers: jsonHeaders(headers),
      body: JSON.stringify({ ...entry, overwrite: true }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`review catalog save ${response.status}: ${text.slice(0, 240)}`);
    }
    const result = await response.json();
    setLiveReviewCatalog(_mergeReviewCatalogEntry(liveReviewCatalog, kind, result.entry as ReviewCatalogEntry));
    return result;
  };

  const runtimeAction: RuntimeActionHandler = async (action) => {
    const response = await fetcher(`${baseUrl}/v1/scillm/exec/${encodeURIComponent(status?.run_id ?? runId)}/actions`, {
      method: "POST",
      headers: jsonHeaders(headers),
      body: JSON.stringify({
        ...action,
        actor: action.actor ?? "scillm-exec-graph-editor",
        provenance: {
          source: "ScillmExecGraphDebuggerLive",
          graph_id: graph.graph_id,
          run_id: status?.run_id ?? runId,
          status_updated_at: status?.updated_at,
          ...(action.provenance ?? {}),
        },
      }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`runtime action ${response.status}: ${text.slice(0, 240)}`);
    }
    return response.json();
  };

  return <ScillmExecGraphDebuggerView graph={graph} status={status} events={events} connection={connection} enablePlanEditing={enablePlanEditing} nicoProposals={nicoProposals} onAmendPlan={onAmendPlan ?? memoryAmendPlan} onRuntimeAction={runtimeAction} amendBackendLabel="ArangoDB through Memory /upsert" amendments={amendments} amendmentsState={amendmentsState} onRefreshAmendments={loadAmendments} onSetAmendmentStatus={memorySetAmendmentStatus} onApplyAmendment={memoryApplyAmendment} availableModels={liveModels} reviewCatalog={liveReviewCatalog} runtimeReadiness={runtimeReadiness} onSaveReviewCatalogEntry={saveReviewCatalogEntry} />;
}

function ScillmExecGraphDebuggerView({
  graph,
  status,
  events = [],
  connection,
  enablePlanEditing = false,
  nicoProposals = [],
  onAmendPlan,
  onRuntimeAction,
  amendBackendLabel = "No amendment backend",
  amendments = [],
  amendmentsState = { status: "idle" },
  onRefreshAmendments,
  onSetAmendmentStatus,
  onApplyAmendment,
  availableModels,
  reviewCatalog,
  runtimeReadiness,
  onSaveReviewCatalogEntry,
}: {
  graph: ExecGraph;
  status?: ExecStatus;
  events?: ExecEvent[];
  connection: ExecGraphDebuggerConnection;
  enablePlanEditing?: boolean;
  nicoProposals?: NicoPlanProposal[];
  onAmendPlan?: AmendPlanHandler;
  onRuntimeAction?: RuntimeActionHandler;
  amendBackendLabel?: string;
  amendments?: ExecGraphAmendment[];
  amendmentsState?: AmendmentsLoadState;
  onRefreshAmendments?: () => void;
  onSetAmendmentStatus?: AmendmentStatusHandler;
  onApplyAmendment?: AmendmentApplyHandler;
  availableModels?: string[];
  reviewCatalog?: ReviewCatalog;
  runtimeReadiness?: RuntimeReadinessReport;
  onSaveReviewCatalogEntry?: SaveReviewCatalogEntryHandler;
}) {
  useRegisterAction("scillm-exec-graph:node:inspect", { app: "scillm", action: "SCILLM_EXEC_NODE_INSPECT", label: "Inspect node" });
  useRegisterAction("scillm-exec-graph:control:pause", { app: "scillm", action: "SCILLM_EXEC_GRAPH_PAUSE", label: "Pause graph" });
  useRegisterAction("scillm-exec-graph:control:resume", { app: "scillm", action: "SCILLM_EXEC_GRAPH_RESUME", label: "Resume graph" });
  useRegisterAction("scillm-exec-graph:control:stop", { app: "scillm", action: "SCILLM_EXEC_GRAPH_STOP", label: "Stop graph" });
  useRegisterAction("scillm-exec-graph:summary:optional-failed", { app: "scillm", action: "SCILLM_EXEC_GRAPH_SELECT_OPTIONAL_FAILURE", label: "Show optional failed node" });
  useRegisterAction("scillm-exec-graph:event:select", { app: "scillm", action: "SCILLM_EXEC_EVENT_SELECT", label: "Select event node" });
  useRegisterAction("scillm-exec-graph:event:filter", { app: "scillm", action: "SCILLM_EXEC_EVENT_FILTER", label: "Filter events" });
  useRegisterAction("scillm-exec-graph:mode:evidence", { app: "scillm", action: "SCILLM_EXEC_GRAPH_MODE_EVIDENCE", label: "Show evidence mode" });
  useRegisterAction("scillm-exec-graph:mode:plan-edit", { app: "scillm", action: "SCILLM_EXEC_GRAPH_MODE_PLAN_EDIT", label: "Show plan edit mode" });
  useRegisterAction("scillm-exec-graph:mode:nico-proposals", { app: "scillm", action: "SCILLM_EXEC_GRAPH_MODE_NICO_PROPOSALS", label: "Show Nico proposals" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-warnings", { app: "scillm", action: "SCILLM_EXEC_PLAN_REVIEW_WARNINGS", label: "Review draft warnings" });
  useRegisterAction("scillm-exec-graph:plan-edit:header-save-amendment", { app: "scillm", action: "SCILLM_EXEC_PLAN_HEADER_SAVE_AMENDMENT", label: "Save draft amendment from header" });
  useRegisterAction("scillm-exec-graph:amendment:load", { app: "scillm", action: "SCILLM_EXEC_AMENDMENT_LOAD_DRAFT", label: "Load amendment draft" });
  useRegisterAction("scillm-exec-graph:amendment:set-status", { app: "scillm", action: "SCILLM_EXEC_AMENDMENT_SET_STATUS", label: "Set amendment status" });
  useRegisterAction("scillm-exec-graph:amendment:apply", { app: "scillm", action: "SCILLM_EXEC_AMENDMENT_APPLY", label: "Apply approved amendment" });

  const { ref, size } = useSize();
  const [selectedId, setSelectedId] = useState(initialSelectedNodeId(graph));
  const [mode, setMode] = useState<DebuggerMode>("evidence");
  const [draftGraph, setDraftGraph] = useState<ExecGraph>(() => cloneExecGraph(graph));
  const [draftHistory, setDraftHistory] = useState<ExecGraph[]>([]);
  const [draftFuture, setDraftFuture] = useState<ExecGraph[]>([]);
  const [lastPlanIssue, setLastPlanIssue] = useState<PlanValidationIssue | undefined>();
  const [appliedProposalIds, setAppliedProposalIds] = useState<Set<string>>(() => new Set());
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [draftAuditLog, setDraftAuditLog] = useState<PlanAuditEntry[]>([]);
  const [amendState, setAmendState] = useState<AmendState>({ status: "idle" });
  const [runtimeActionState, setRuntimeActionState] = useState<{ status: "idle" | "running" | "ok" | "error"; message?: string }>({ status: "idle" });
  const [warningsAcknowledged, setWarningsAcknowledged] = useState(false);
  const [formalDiffCopied, setFormalDiffCopied] = useState(false);
  const activeGraph = mode === "evidence" ? graph : draftGraph;
  const states = useMemo(() => buildStates(graph, status, events), [events, graph, status]);
  const draftStates = useMemo(() => buildStates(draftGraph, status, events), [draftGraph, events, status]);
  const activeStates = mode === "evidence" ? states : draftStates;
  const { nodes, edges } = useMemo(() => layout(activeGraph, activeStates, size.width, size.height, selectedId), [activeGraph, activeStates, selectedId, size.height, size.width]);
  const selected = activeGraph.nodes.find((node) => node.id === selectedId) ?? activeGraph.nodes[0];
  const selectedResult = selected ? nodeResult(status, selected.id) : undefined;
  const summary = useMemo(() => runSummary(graph, status, states), [graph, status, states]);
  const planValidation = useMemo(() => validateExecGraphPlan(draftGraph), [draftGraph]);
  const planDiff = useMemo(() => diffExecGraphPlan(graph, draftGraph), [draftGraph, graph]);
  const draftRuntimeReadiness = useMemo(() => runtimeReadiness ?? analyzeExecGraphRuntimeReadiness(draftGraph), [draftGraph, runtimeReadiness]);
  const planDirty = planDiff.length > 0;
  const isCompleted = summary.lifecycle === "completed";
  const isTerminal = ["completed", "stopped", "failed", "cancelled"].includes(summary.lifecycle);
  const verdict = currentVerdict(summary, isTerminal);
  const filteredEvents = eventFilter === "all" ? events : events.filter((event) => event.state === eventFilter || eventTone(event) === eventFilter);
  const visibleEvents = filteredEvents.slice(-12).reverse();
  const statusTitle = [
    `Lifecycle: ${titleCase(summary.lifecycle)}`,
    `${verdict.label}: ${verdict.text}`,
    runImpact(summary),
    connection.updated_at ? `${isCompleted ? "UI last refreshed at" : "Auto-refresh checked at"} ${formatTimestamp(connection.updated_at)}` : "",
    connection.error ? `Error: ${connection.error}` : "",
  ].filter(Boolean).join("\n");
  const firstOptionalFailed = graph.nodes.find((node) => states[node.id] === "failed" && isOptionalNode(node, nodeResult(status, node.id)));
  const completedControlsReasonId = "graph-controls-disabled-reason";
  const runtimeControlsUnavailable = !onRuntimeAction || isTerminal || runtimeActionState.status === "running";
  const runtimeControlsReason = isTerminal
    ? "Controls unavailable because the run is terminal."
    : !onRuntimeAction
      ? "Runtime controls require a connected backend action handler."
      : runtimeActionState.status === "running"
        ? "Runtime action is being submitted."
        : "";
  const selectedEvidenceDefect = selected ? requiredEvidenceDefect(selected, selectedResult, activeStates[selected.id] ?? "pending") : false;
  const selectedDependencyNodes = selected
    ? (selected.depends_on ?? []).map((id) => activeGraph.nodes.find((node) => node.id === id)).filter((node): node is ExecGraphNode => Boolean(node))
    : [];
  const selectedDependentNodes = selected
    ? activeGraph.nodes.filter((node) => (node.depends_on ?? []).includes(selected.id))
    : [];
  const selectedNodeEvents = selected ? events.filter((event) => event.node_id === selected.id) : [];
  const headerAmendDisabled = !planDirty || !onAmendPlan || planValidation.blocking.length > 0 || (planValidation.warnings.length > 0 && !warningsAcknowledged);
  const headerAmendTitle = !planDirty
    ? "No draft changes to save"
    : !onAmendPlan
      ? "No amendment backend is connected"
      : planValidation.blocking.length > 0
        ? "Resolve blocking plan validation issues before saving"
        : planValidation.warnings.length > 0 && !warningsAcknowledged
          ? "Review and acknowledge warnings in Plan edit before saving"
          : "Save current draft amendment to Memory";
  const headerAmendReason = planDirty
    ? planValidation.blocking.length > 0
      ? `Save blocked: ${planValidation.blocking.length} blocking issue${planValidation.blocking.length === 1 ? "" : "s"}`
      : planValidation.warnings.length > 0 && !warningsAcknowledged
        ? `Save blocked: acknowledge ${planValidation.warnings.length} warning${planValidation.warnings.length === 1 ? "" : "s"}`
        : "Save ready: amendment will preserve warning/provenance evidence"
    : "";

  useEffect(() => {
    setDraftGraph(cloneExecGraph(graph));
    setDraftHistory([]);
    setDraftFuture([]);
    setLastPlanIssue(undefined);
    setAppliedProposalIds(new Set());
    setDraftAuditLog([]);
    setAmendState({ status: "idle" });
  }, [graph]);

  useEffect(() => {
    if (!graph.nodes.some((node) => node.id === selectedId)) setSelectedId(initialSelectedNodeId(graph));
  }, [graph, selectedId]);

  useEffect(() => {
    setWarningsAcknowledged(false);
  }, [planValidation.warnings.length, planDirty]);

  useEffect(() => {
    if (mode !== "evidence" && !enablePlanEditing) setMode("evidence");
  }, [enablePlanEditing, mode]);

  useEffect(() => {
    if (activeGraph.nodes.some((node) => node.id === selectedId)) return;
    setSelectedId(activeGraph.nodes[0]?.id ?? "");
  }, [activeGraph.nodes, selectedId]);

  useEffect(() => {
    if (!isCompleted || !firstOptionalFailed) return;
    if (mode !== "evidence") return;
    if (selectedId !== graph.nodes[0]?.id) return;
    setSelectedId(firstOptionalFailed.id);
  }, [firstOptionalFailed, graph.nodes, isCompleted, mode, selectedId]);

  function patchDraft(patch: Parameters<typeof applyPlanPatch>[1]) {
    setDraftGraph((current) => {
      const result = applyPlanPatch(current, patch);
      setLastPlanIssue(result.issue);
      if (!result.applied) {
        if (result.issue) {
          setDraftAuditLog((log) => [rejectedPatchAuditEntry("local editor", patch, current, result.issue!, selectedId), ...log].slice(0, 12));
        }
        return current;
      }
      setDraftHistory((history) => [...history, cloneExecGraph(current)]);
      setDraftFuture([]);
      setDraftAuditLog((log) => [patchAuditEntry("local editor", patch, current, result.graph), ...log].slice(0, 12));
      setAmendState({ status: "idle" });
      setWarningsAcknowledged(false);
      return result.graph;
    });
  }

  function applyProposal(proposal: NicoPlanProposal) {
    setDraftGraph((current) => {
      const result = applyNicoPlanProposal(current, proposal);
      setLastPlanIssue(result.issue);
      if (!result.applied) return current;
      setDraftHistory((history) => [...history, cloneExecGraph(current)]);
      setDraftFuture([]);
      setAppliedProposalIds((ids) => new Set(ids).add(proposal.id));
      setAmendState({ status: "idle" });
      setWarningsAcknowledged(false);
      setDraftAuditLog((log) => [
        {
          id: `${new Date().toISOString()}-proposal-${proposal.id}`,
          ts: new Date().toISOString(),
          actor: proposal.proposed_by,
          action: "proposal applied",
          details: `${proposal.title} · ${proposal.patches.length} patch${proposal.patches.length === 1 ? "" : "es"}`,
          diffRefs: diffRefsForChange(current, result.graph),
          before: current,
          after: result.graph,
        },
        ...log,
      ].slice(0, 12));
      return result.graph;
    });
  }

  function loadAmendmentDraft(amendment: ExecGraphAmendment) {
    if (!amendment.draft_graph) return;
    setMode("plan_edit");
    setDraftGraph(cloneExecGraph(amendment.draft_graph));
    setDraftHistory((history) => [...history, cloneExecGraph(draftGraph)]);
    setDraftFuture([]);
    setLastPlanIssue(undefined);
    setAmendState({
      status: "saved",
      message: `Loaded amendment ${amendment._key} from Memory.`,
      amendment_key: amendment._key,
      local_amendment_id: amendment._key,
      diff_hash: localIdentity(amendment.diff ?? []),
      saved_at: amendment.updated_at ?? amendment.created_at ?? new Date().toISOString(),
      graph_id: amendment.graph_id,
      diff_count: amendment.diff?.length ?? diffExecGraphPlan(graph, amendment.draft_graph).length,
      acknowledged_warning_ids: [],
      proposal_ids: [],
    });
    setDraftAuditLog((log) => [
      {
        id: `${new Date().toISOString()}-load-amendment-${amendment._key}`,
        ts: new Date().toISOString(),
        actor: amendment.actor ?? "Memory",
        action: "amendment loaded",
        details: `${amendment._key} · ${amendment.status}`,
        before: draftGraph,
        after: amendment.draft_graph,
      },
      ...log,
    ].slice(0, 12));
  }

  function undoDraft() {
    setDraftHistory((history) => {
      const previous = history[history.length - 1];
      if (!previous) return history;
      setDraftGraph((current) => {
        setDraftFuture((future) => [cloneExecGraph(current), ...future]);
        setDraftAuditLog((log) => [{
          id: `${new Date().toISOString()}-undo-draft`,
          ts: new Date().toISOString(),
          actor: "local editor",
          action: "undo",
          details: "Returned to previous draft revision.",
          before: current,
          after: previous,
        }, ...log].slice(0, 12));
        setAmendState({ status: "idle" });
        setWarningsAcknowledged(false);
        return cloneExecGraph(previous);
      });
      return history.slice(0, -1);
    });
    setLastPlanIssue(undefined);
  }

  function redoDraft() {
    setDraftFuture((future) => {
      const next = future[0];
      if (!next) return future;
      setDraftGraph((current) => {
        setDraftHistory((history) => [...history, cloneExecGraph(current)]);
        setDraftAuditLog((log) => [{
          id: `${new Date().toISOString()}-redo-draft`,
          ts: new Date().toISOString(),
          actor: "local editor",
          action: "redo",
          details: "Restored next draft revision.",
          before: current,
          after: next,
        }, ...log].slice(0, 12));
        setAmendState({ status: "idle" });
        setWarningsAcknowledged(false);
        return cloneExecGraph(next);
      });
      return future.slice(1);
    });
    setLastPlanIssue(undefined);
  }

  async function amendDraft() {
    if (!onAmendPlan || !planValidation.canApply || !planDirty || amendState.status === "saving") return;
    if (planValidation.warnings.length && !warningsAcknowledged) return;
    const warningAcceptance = planValidation.warnings.length
      ? {
        accepted: true,
        actor: "scillm-exec-graph-editor",
        accepted_at: new Date().toISOString(),
        warning_ids: planValidation.warnings.map(warningIdentity),
        acknowledgement_version: "scillm.exec.graph.warning_ack.v1",
        acknowledgement_text: planValidation.warnings.map((issue) => issue.code === "missing_prompt_contract" ? `I acknowledge this amendment is being saved with missing prompt contract warning for ${issue.node_id ?? "graph"}.` : `I acknowledge this amendment is being saved with ${issue.code} warning for ${issue.node_id ?? "graph"}.`),
        warnings: planValidation.warnings,
      }
      : undefined;
    setAmendState({ status: "saving", message: "Saving amendment record to shared Memory." });
    try {
      const result = await onAmendPlan(draftGraph, { baseGraph: graph, diff: planDiff, validation: planValidation, warning_acceptance: warningAcceptance });
      const maybeResult = result as { amendment_key?: string } | undefined;
      setAmendState({
        status: "saved",
        message: maybeResult?.amendment_key ? `Saved to Memory: ${maybeResult.amendment_key}` : "Saved to shared Memory.",
        amendment_key: maybeResult?.amendment_key,
        local_amendment_id: maybeResult?.amendment_key ?? localIdentity({ graph_id: graph.graph_id, diff: planDiff, actor: "scillm-exec-graph-editor" }),
        diff_hash: localIdentity(planDiff),
        saved_at: new Date().toISOString(),
        graph_id: graph.graph_id,
        diff_count: planDiff.length,
        acknowledged_warning_ids: planValidation.warnings.map(warningIdentity),
        proposal_ids: Array.from(appliedProposalIds),
      });
    } catch (error) {
      setAmendState({ status: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }

  async function dispatchRuntimeAction(action: RuntimeActionRequest, label: string) {
    if (!onRuntimeAction || runtimeActionState.status === "running") return;
    setRuntimeActionState({ status: "running", message: `${label} requested.` });
    try {
      await onRuntimeAction(action);
      setRuntimeActionState({ status: "ok", message: `${label} accepted by runtime.` });
    } catch (error) {
      setRuntimeActionState({ status: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <section className="scillm-exec-debugger" data-qid="scillm-exec-graph:debugger" aria-label="scillm exec graph debugger">
      <style>{execGraphDebuggerCss}</style>
      <div style={{ display: "grid", gridTemplateRows: "auto minmax(360px, 1fr) auto", alignContent: "start", minWidth: 0, minHeight: 0 }}>
        <header style={{ padding: 16, background: "var(--exec-panel, #151923)", borderBottom: "1px solid var(--exec-border, rgba(255,255,255,0.14))" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <div>
              <div style={{ color: dimColor, fontSize: 12, letterSpacing: 0, textTransform: "uppercase" }}>scillm exec graph</div>
              <h2 style={{ margin: "4px 0 0", fontSize: 18 }}>{graph.graph_id}</h2>
              <div data-qid="scillm-exec-graph:live-status" title={statusTitle} style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, minHeight: 24, color: connection.state === "error" ? "var(--exec-failed, #ef4444)" : dimColor, fontSize: 12, flexWrap: "wrap" }}>
                <span aria-hidden style={{ width: 8, height: 8, borderRadius: 999, background: summary.requiredFailed > 0 || summary.requiredEvidenceDefects > 0 ? "var(--exec-failed, #ef4444)" : summary.optionalFailed > 0 ? "var(--exec-warning, #facc15)" : connection.state === "live" ? "var(--exec-passed, #22c55e)" : connection.state === "error" ? "var(--exec-failed, #ef4444)" : "var(--exec-running, #f59e0b)" }} />
                <strong style={{ color: connection.state === "error" ? "var(--exec-failed, #ef4444)" : "var(--exec-text, #e5e7eb)", fontWeight: 600 }}>Lifecycle: {titleCase(summary.lifecycle)}</strong>
                <strong style={{ color: summary.requiredFailed > 0 || summary.requiredEvidenceDefects > 0 ? "var(--exec-failed, #ef4444)" : summary.optionalFailed > 0 ? "var(--exec-warning, #facc15)" : "var(--exec-passed, #22c55e)", fontWeight: 600 }}>{verdict.label}: {verdict.text}</strong>
                <span className={summary.requiredFailed > 0 || summary.requiredEvidenceDefects > 0 ? "exec-verdict-impact exec-verdict-impact-failed" : "exec-verdict-impact"}>{runImpact(summary)}</span>
                {connection.error ? <span style={{ color: "var(--exec-failed, #ef4444)", maxWidth: 420, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{connection.error}</span> : null}
              </div>
              {connection.updated_at ? <div style={{ marginTop: 4, color: dimColor, fontSize: 12 }}>{isCompleted ? "UI last refreshed at" : "Auto-refresh checked at"} {formatTimestamp(connection.updated_at)}</div> : null}
            </div>
            <div className="exec-controls-cluster">
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
                <button className="exec-control-button" data-qid="scillm-exec-graph:control:pause" data-qs-action="SCILLM_EXEC_GRAPH_PAUSE" title={runtimeControlsReason || "Pause graph scheduling"} aria-label={runtimeControlsReason || "Pause graph scheduling"} aria-describedby={runtimeControlsUnavailable ? completedControlsReasonId : undefined} disabled={runtimeControlsUnavailable} aria-disabled={runtimeControlsUnavailable} onClick={() => void dispatchRuntimeAction({ action: "pause", target: "graph", reason: "Pause graph scheduling from DAG viewer." }, "Pause graph")}>Pause</button>
                <button className="exec-control-button" data-qid="scillm-exec-graph:control:resume" data-qs-action="SCILLM_EXEC_GRAPH_RESUME" title={runtimeControlsReason || "Resume graph scheduling"} aria-label={runtimeControlsReason || "Resume graph scheduling"} aria-describedby={runtimeControlsUnavailable ? completedControlsReasonId : undefined} disabled={runtimeControlsUnavailable} aria-disabled={runtimeControlsUnavailable} onClick={() => void dispatchRuntimeAction({ action: "resume", target: "graph", reason: "Resume graph scheduling from DAG viewer." }, "Resume graph")}>Resume</button>
                <button className="exec-control-button exec-control-button-danger" data-qid="scillm-exec-graph:control:stop" data-qs-action="SCILLM_EXEC_GRAPH_STOP" title={runtimeControlsReason || "Stop graph run"} aria-label={runtimeControlsReason || "Stop graph run"} aria-describedby={runtimeControlsUnavailable ? completedControlsReasonId : undefined} disabled={runtimeControlsUnavailable} aria-disabled={runtimeControlsUnavailable} onClick={() => void dispatchRuntimeAction({ action: "stop", target: "graph", reason: "Stop graph run from DAG viewer." }, "Stop graph")}>Stop</button>
              </div>
              {runtimeControlsUnavailable ? <div id={completedControlsReasonId} className="exec-controls-reason">{runtimeControlsReason}</div> : null}
              {runtimeActionState.message ? <div className={runtimeActionState.status === "error" ? "exec-controls-reason exec-controls-reason-error" : "exec-controls-reason"}>{runtimeActionState.message}</div> : null}
              {status?.paused || status?.disabled_node_ids?.length || status?.runtime_actions?.length ? (
                <div className="exec-controls-reason" data-qid="scillm-exec-graph:control:ledger-summary">
                  {status.paused ? "Paused" : "Running"} · {status.disabled_node_ids?.length ?? 0} disabled · {status.runtime_actions?.length ?? 0} actions
                </div>
              ) : null}
            </div>
          </div>
          <p style={{ margin: "8px 0 0", color: dimColor, fontSize: 13, lineHeight: 1.45 }}>{graph.graph_goal}</p>
          {enablePlanEditing ? (
            <div className="exec-mode-row" data-qid="scillm-exec-graph:mode-tabs">
              <div className="exec-mode-tabs" role="tablist" aria-label="DAG debugger mode">
                <button type="button" className={mode === "evidence" ? "exec-mode-tab exec-mode-tab-active" : "exec-mode-tab"} role="tab" aria-selected={mode === "evidence"} data-qid="scillm-exec-graph:mode:evidence" data-qs-action="SCILLM_EXEC_GRAPH_MODE_EVIDENCE" title="Show immutable execution evidence" onClick={() => setMode("evidence")}>Evidence</button>
                <button type="button" className={mode === "plan_edit" ? "exec-mode-tab exec-mode-tab-active" : "exec-mode-tab"} role="tab" aria-selected={mode === "plan_edit"} data-qid="scillm-exec-graph:mode:plan-edit" data-qs-action="SCILLM_EXEC_GRAPH_MODE_PLAN_EDIT" title={planDirty ? "Show draft plan editor; unsaved draft changes exist" : "Show draft plan editor"} onClick={() => setMode("plan_edit")}>Plan edit{planDirty ? " *" : ""}</button>
                <button type="button" className={mode === "nico_proposals" ? "exec-mode-tab exec-mode-tab-active" : "exec-mode-tab"} role="tab" aria-selected={mode === "nico_proposals"} data-qid="scillm-exec-graph:mode:nico-proposals" data-qs-action="SCILLM_EXEC_GRAPH_MODE_NICO_PROPOSALS" title="Show Nico plan proposals" onClick={() => setMode("nico_proposals")}>Nico proposals</button>
              </div>
              <span className={!planValidation.canApply ? "exec-plan-chip exec-plan-chip-blocking" : planDirty ? "exec-plan-chip exec-plan-chip-dirty" : "exec-plan-chip exec-plan-chip-ok"} data-qid="scillm-exec-graph:plan-validation-chip">
                {planDirty ? "Unsaved draft · " : ""}{planValidation.blocking.length} blocking · {planValidation.warnings.length} warnings · {planDiff.length} changes
              </span>
              <span className={draftRuntimeReadiness.can_execute_runtime ? "exec-plan-chip exec-plan-chip-ok" : "exec-plan-chip exec-plan-chip-blocking"} data-qid="scillm-exec-graph:runtime-readiness-chip" title="Plan-iterate execution readiness for the current draft graph">
                {draftRuntimeReadiness.summary.blocked_node_count} missing-field nodes · {draftRuntimeReadiness.summary.manual_node_count} manual
              </span>
              {mode === "evidence" ? <span className="exec-plan-chip exec-plan-chip-readonly">Read-only evidence</span> : null}
              {selectedEvidenceDefect ? <span className="exec-plan-chip exec-plan-chip-blocking" data-qid="scillm-exec-graph:evidence-defect-chip">Selected node needs hash/artifact evidence</span> : null}
              {planDirty ? (
                <span className="exec-header-draft-actions" data-qid="scillm-exec-graph:plan-edit:header-actions">
                  <span className={!headerAmendDisabled ? "exec-header-save-precondition exec-header-save-precondition-ready" : "exec-header-save-precondition"} data-qid="scillm-exec-graph:plan-edit:save-precondition">{headerAmendReason}</span>
                  <button type="button" className="exec-control-button exec-control-button-compact" data-qid="scillm-exec-graph:plan-edit:review-warnings" data-qs-action="SCILLM_EXEC_PLAN_REVIEW_WARNINGS" title="Open Plan edit to review draft validation warnings and actions" onClick={() => setMode("plan_edit")}>Review warnings</button>
                  <button type="button" className="exec-control-button exec-control-button-compact" data-qid="scillm-exec-graph:plan-edit:header-save-amendment" data-qs-action="SCILLM_EXEC_PLAN_HEADER_SAVE_AMENDMENT" title={headerAmendTitle} disabled={headerAmendDisabled} aria-disabled={headerAmendDisabled} onClick={() => void amendDraft()}>{amendState.status === "saving" ? "Saving..." : "Save amendment"}</button>
                </span>
              ) : null}
            </div>
          ) : null}
          <div data-qid="scillm-exec-graph:run-summary" title="Run result summary" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 12 }}>
            <span className="exec-summary-label">Summary</span>
            <span className="exec-summary-chip exec-summary-chip-passed">{summary.passed} passed</span>
            {summary.optionalFailed ? (
              <>
                <span className="exec-summary-chip exec-summary-chip-warning">{summary.optionalFailed} optional failed</span>
                <button className="exec-summary-action-button" type="button" data-qid="scillm-exec-graph:summary:optional-failed" data-qs-action="SCILLM_EXEC_GRAPH_SELECT_OPTIONAL_FAILURE" title="Focus optional failure node" aria-label={`Focus optional failure node${summary.optionalFailed === 1 ? "" : "s"}`} onClick={() => {
                  if (firstOptionalFailed) setSelectedId(firstOptionalFailed.id);
                }}>Focus optional failure</button>
              </>
            ) : null}
            {summary.requiredFailed ? <span className="exec-summary-chip exec-summary-chip-failed">{summary.requiredFailed} required failed</span> : null}
            {summary.requiredEvidenceDefects ? <span className="exec-summary-chip exec-summary-chip-failed" title={summary.requiredEvidenceDefectNodes.join(", ")}>{summary.requiredEvidenceDefects} execution passed, evidence incomplete</span> : null}
            <span className="exec-summary-chip">{summary.running} running</span>
          </div>
          {summary.requiredEvidenceDefects ? (
            <div className="exec-evidence-defect-queue" data-qid="scillm-exec-graph:evidence-defect-queue" title="Required evidence defect queue">
              <strong>Evidence defects</strong>
              <span>{summary.requiredEvidenceDefects} required nodes missing hash/artifact evidence</span>
              <div className="exec-evidence-defect-list">
                {summary.requiredEvidenceDefectNodes.map((nodeId) => (
                  <button
                    key={nodeId}
                    type="button"
                    className={nodeId === selectedId ? "exec-evidence-defect-item exec-evidence-defect-item-selected" : "exec-evidence-defect-item"}
                    data-qid={`scillm-exec-graph:evidence-defect:${nodeId}`}
                    data-qs-action="SCILLM_EXEC_SELECT_EVIDENCE_DEFECT"
                    title={`Focus evidence defect ${nodeId}`}
                    onClick={() => setSelectedId(nodeId)}
                  >
                    <span>{nodeId}</span>
                    <b>hash/artifact missing</b>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </header>

        <div ref={ref} data-qid="scillm-exec-graph:canvas" title="Execution graph canvas" style={{ minHeight: 0, position: "relative" }}>
          <div className="exec-canvas-legend">{mode === "evidence" ? "Evidence edges" : "Draft dependencies"} <span aria-hidden>→</span><span>arrows point to dependent work</span></div>
          <div className="exec-canvas-keyboard-hint">Keyboard: Tab to a node, arrows move selection, Enter or Space inspects.</div>
          {selected ? (
            <div className="exec-selected-neighborhood" data-qid="scillm-exec-graph:selected-node:neighborhood">
              <strong>Selected graph context</strong>
              <span>{selected.id}</span>
              <span className="exec-neighborhood-group">
                <b>Depends on</b>
                {selectedDependencyNodes.length ? selectedDependencyNodes.map((node) => (
                  <button key={node.id} type="button" className="exec-neighborhood-chip" onClick={() => setSelectedId(node.id)} title={`Select dependency ${node.id}`}>{node.id}</button>
                )) : <em>none</em>}
              </span>
              <span className="exec-neighborhood-group">
                <b>Feeds</b>
                {selectedDependentNodes.length ? selectedDependentNodes.map((node) => (
                  <button key={node.id} type="button" className="exec-neighborhood-chip" onClick={() => setSelectedId(node.id)} title={`Select dependent node ${node.id}`}>{node.id}</button>
                )) : <em>none</em>}
              </span>
            </div>
          ) : null}
          <svg role="img" aria-label={mode === "evidence" ? "Live scillm exec DAG" : "Draft scillm exec DAG"} viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="xMidYMid meet" style={{ display: "block", width: "100%", height: "100%" }}>
            <defs><marker id="exec-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--exec-dim, #94a3b8)" /></marker></defs>
            <g aria-hidden="true">{edges.map((edge) => {
              const selectedEdge = edge.source.id === selected?.id || edge.target.id === selected?.id;
              const labelX = (edge.source.x + edge.target.x) / 2;
              const labelY = (edge.source.y + edge.target.y) / 2 - 8;
              return (
                <g key={edge.id}>
                  <path d={edge.path} fill="none" stroke={selectedEdge ? "var(--exec-selected-ring, #22d3ee)" : "var(--exec-edge, #6b7280)"} strokeWidth={selectedEdge ? 2.5 : 1.75} opacity={selectedEdge ? 0.95 : 0.72} markerEnd="url(#exec-arrow)" />
                  <text className={selectedEdge ? "exec-edge-label exec-edge-label-selected" : "exec-edge-label"} x={labelX} y={labelY} textAnchor="middle">depends on</text>
                </g>
              );
            })}</g>
            <g>{nodes.map((node, index) => {
              const nodeIssues = mode === "evidence" ? [] : planValidation.issues.filter((issue) => issue.node_id === node.id);
              const semanticEvents = events.filter((event) => event.node_id === node.id && (event.event_type || event.type === "agent_event" || event.type.startsWith("subagent_")));
              return <GraphNode key={node.id} node={node} result={nodeResult(status, node.id)} optional={isOptionalNode(node, nodeResult(status, node.id))} selected={node.id === selected?.id} validationIssues={nodeIssues} latestSemanticEvent={semanticEvents.at(-1)} semanticEventCount={semanticEvents.length} onSelect={() => setSelectedId(node.id)} onSelectAdjacent={(direction) => {
              const next = nodes[(index + direction + nodes.length) % nodes.length];
              if (next) setSelectedId(next.id);
            }} />;
            })}</g>
          </svg>
        </div>

        <footer style={{ padding: 12, background: "var(--exec-panel, #151923)", borderTop: "1px solid var(--exec-border, rgba(255,255,255,0.14))" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 14, color: "var(--exec-dim-contrast)", fontSize: 13, lineHeight: "18px", fontWeight: 600, marginBottom: 10, flexWrap: "wrap" }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              <span>Recent events · time UTC; full timestamp on hover</span>
              <StateLegend />
              <label className="exec-event-filter">
                <span>Filter</span>
                <select
                  value={eventFilter}
                  className="exec-plan-input exec-event-filter-select"
                  data-qid="scillm-exec-graph:event:filter"
                  data-qs-action="SCILLM_EXEC_EVENT_FILTER"
                  title="Filter recent events by state"
                  onChange={(event) => setEventFilter(event.target.value as EventFilter)}
                >
                  <option value="all">All</option>
                  <option value="passed">Passed</option>
                  <option value="running">Running</option>
                  <option value="failed">Failed</option>
                  <option value="pending">Pending</option>
                  <option value="needs_attention">Needs attention</option>
                  <option value="skipped">Skipped</option>
                  <option value="paused">Paused</option>
                  <option value="stopped">Stopped</option>
                </select>
              </label>
            </div>
            <span>Showing {visibleEvents.length} of {filteredEvents.length} filtered events · {activeGraph.nodes.length} nodes</span>
          </div>
          {enablePlanEditing && mode !== "evidence" ? (
            <PlanDraftPanel
              mode={mode}
              validation={planValidation}
              diff={planDiff}
              runtimeReadiness={draftRuntimeReadiness}
              dirty={planDirty}
              lastIssue={lastPlanIssue}
              nicoProposals={nicoProposals}
              draftGraph={draftGraph}
              auditLog={draftAuditLog}
              appliedProposalIds={appliedProposalIds}
              canAmend={Boolean(onAmendPlan)}
              graphId={graph.graph_id}
              amendBackendLabel={amendBackendLabel}
              amendState={amendState}
              warningsAcknowledged={warningsAcknowledged}
              onWarningsAcknowledgedChange={setWarningsAcknowledged}
              formalDiffCopied={formalDiffCopied}
              amendments={amendments}
              amendmentsState={amendmentsState}
              canUndo={draftHistory.length > 0}
              canRedo={draftFuture.length > 0}
              onUndo={undoDraft}
              onRedo={redoDraft}
              onReset={() => {
                const original = cloneExecGraph(graph);
                setDraftAuditLog((log) => [resetAuditEntry(draftGraph, original), ...log].slice(0, 12));
                setDraftGraph(cloneExecGraph(graph));
                setDraftHistory([]);
                setDraftFuture([]);
                setLastPlanIssue(undefined);
                setAppliedProposalIds(new Set());
                setAmendState({ status: "idle" });
                setWarningsAcknowledged(false);
              }}
              onExportDiff={() => {
                void navigator.clipboard?.writeText(JSON.stringify({
                  graph_id: graph.graph_id,
                  actor: "scillm-exec-graph-editor",
                  timestamp: new Date().toISOString(),
                  base_graph_revision: status?.updated_at ?? null,
                  amendment_id: amendState.status === "saved" ? amendState.local_amendment_id : null,
                  diff_hash: localIdentity(planDiff),
                  warning_acknowledgements: planValidation.warnings.map((issue) => ({ id: warningIdentity(issue), code: issue.code, node_id: issue.node_id ?? null, acknowledged: warningsAcknowledged })),
                  proposal_provenance: Array.from(appliedProposalIds),
                  base_graph: graph,
                  draft_graph: draftGraph,
                  diff: planDiff,
                }, null, 2));
                setFormalDiffCopied(true);
                window.setTimeout(() => setFormalDiffCopied(false), 1400);
              }}
              onAmend={() => void amendDraft()}
              onRefreshAmendments={onRefreshAmendments}
              onLoadAmendment={loadAmendmentDraft}
              onSetAmendmentStatus={onSetAmendmentStatus}
              onApplyAmendment={onApplyAmendment}
              onApplyProposal={applyProposal}
              onSelectNode={setSelectedId}
            />
          ) : null}
          <div className="exec-events-list">{visibleEvents.map((event, index) => {
            const closureBlockedEvent = !event.node_id && event.type.includes("graph_completed") && summary.requiredEvidenceDefects > 0;
            const canSelect = Boolean(event.node_id && activeGraph.nodes.some((node) => node.id === event.node_id)) || closureBlockedEvent;
            const eventText = !event.node_id && event.type.includes("graph_completed") && summary.requiredEvidenceDefects > 0
              ? "Closure blocked: evidence gate blocked despite graph_completed execution event"
              : event.text ?? "system";
            const eventTitle = closureBlockedEvent
              ? "Focus evidence-defect queue and first blocked node"
              : event.node_id
                ? `Select node ${event.node_id}`
                : "System event";
            return (
            <button
              key={`${event.ts ?? "event"}-${index}`}
              type="button"
              className={event.node_id === selectedId ? "exec-event-row exec-event-row-selected" : closureBlockedEvent ? "exec-event-row exec-event-row-blocked" : "exec-event-row"}
              data-qid={`scillm-exec-graph:event:${event.node_id ?? "system"}:${index}`}
              data-qs-action="SCILLM_EXEC_EVENT_SELECT"
              title={[event.ts, event.type, eventTitle, event.text].filter(Boolean).join(" · ")}
              disabled={!canSelect}
              aria-disabled={!canSelect}
              aria-current={event.node_id === selectedId ? "true" : undefined}
              onClick={() => {
              if (event.node_id) setSelectedId(event.node_id);
              else if (closureBlockedEvent) {
                if (summary.requiredEvidenceDefectNodes[0]) setSelectedId(summary.requiredEvidenceDefectNodes[0]);
                document.querySelector('[data-qid="scillm-exec-graph:evidence-defect-queue"]')?.scrollIntoView({ block: "nearest" });
              }
            }}>
              <span aria-hidden style={{ width: 7, height: 7, borderRadius: 999, background: closureBlockedEvent ? "var(--exec-failed, #ef4444)" : stateColor[eventTone(event)] }} />
              <span className="exec-event-time">{formatEventTime(event.ts)}</span>
              <strong>{event.type}</strong>
              {closureBlockedEvent ? <b className="exec-event-blocked-badge">CLOSURE BLOCKED</b> : null}
              <span>{event.node_id ? `${event.node_id}${event.text ? ` · ${event.text}` : ""}` : eventText}</span>
            </button>
          );})}</div>
        </footer>
      </div>

      <aside data-qid="scillm-exec-graph:node-inspector" style={{ background: "var(--exec-panel, #151923)", borderLeft: "1px solid var(--exec-border, rgba(255,255,255,0.14))", overflow: "auto" }}>
        {selected ? (
          <Inspector
            node={selected}
            state={activeStates[selected.id] ?? "pending"}
            result={selectedResult}
            runId={status?.run_id}
            statusUpdatedAt={status?.updated_at}
            optional={isOptionalNode(selected, selectedResult)}
            onSelectNode={setSelectedId}
            mode={mode}
            allNodes={activeGraph.nodes}
            validation={planValidation}
            diff={planDiff}
            runtimeReadinessNode={mode === "evidence" ? undefined : draftRuntimeReadiness.nodes.find((report) => report.node_id === selected.id)}
            onUpdateNode={mode === "plan_edit" ? (fields) => patchDraft({ op: "update_node", node_id: selected.id, fields }) : undefined}
            onAddDependency={mode === "plan_edit" ? (dependency) => patchDraft({ op: "add_dependency", node_id: selected.id, depends_on: dependency }) : undefined}
            onRemoveDependency={mode === "plan_edit" ? (dependency) => patchDraft({ op: "remove_dependency", node_id: selected.id, depends_on: dependency }) : undefined}
            nodeEvents={selectedNodeEvents}
            availableModels={availableModels}
            reviewCatalog={reviewCatalog}
            onSaveReviewCatalogEntry={onSaveReviewCatalogEntry}
          />
        ) : <div style={{ padding: 16 }}>Select a node.</div>}
      </aside>
    </section>
  );
}

function StateLegend() {
  const states: ExecNodeState[] = ["passed", "running", "failed", "pending", "needs_attention", "skipped"];
  return (
    <span aria-label="Node state legend" style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      {states.map((state) => (
        <span key={state} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <span aria-hidden style={{ width: 7, height: 7, borderRadius: 999, background: stateColor[state] }} />
          <span>{stateLabel[state]}</span>
        </span>
      ))}
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <span aria-hidden style={{ width: 14, height: 10, borderRadius: 999, border: "2px solid var(--exec-selected-ring, #7aa2ff)" }} />
        <span>Selected node</span>
      </span>
      <span>Edges: depends on</span>
    </span>
  );
}

function GraphNode({ node, result, optional, selected, validationIssues, latestSemanticEvent, semanticEventCount = 0, onSelect, onSelectAdjacent }: { node: LayoutNode; result?: Record<string, unknown>; optional: boolean; selected: boolean; validationIssues: PlanValidationIssue[]; latestSemanticEvent?: ExecEvent; semanticEventCount?: number; onSelect: () => void; onSelectAdjacent: (direction: -1 | 1) => void }) {
  const evidenceDefect = requiredEvidenceDefect(node, result, node.state);
  const statusText = evidenceDefect ? "Evidence incomplete" : optional && node.state === "failed" ? "Optional failed" : stateLabel[node.state];
  const latestSemanticLabel = latestSemanticEvent?.event_type ?? latestSemanticEvent?.type;
  const hasBlockingIssue = validationIssues.some((issue) => issue.severity === "blocking");
  const hasValidationIssue = validationIssues.length > 0;
  const blockingCount = validationIssues.filter((issue) => issue.severity === "blocking").length;
  const optionalBorder = optional ? "2px dashed var(--exec-optional-border, #9ca35a)" : `2px solid ${stateColor[node.state]}`;
  const validationBorder = evidenceDefect ? "3px solid var(--exec-failed, #ef4444)" : hasBlockingIssue ? "3px solid var(--exec-failed, #ef4444)" : hasValidationIssue ? "2px dashed var(--exec-warning, #facc15)" : optionalBorder;
  const nodeClassName = [
    "exec-node-button",
    selected ? "exec-node-button-selected" : "",
    hasBlockingIssue || evidenceDefect ? "exec-node-button-blocking" : "",
  ].filter(Boolean).join(" ");

  return (
    <foreignObject x={node.x - nodeWidth / 2 - 8} y={node.y - nodeHeight / 2 - 8} width={nodeWidth + 16} height={nodeHeight + 16}>
      <button
        className={nodeClassName}
        data-qid={`scillm-exec-graph:node:${node.id}`}
        data-qs-action="SCILLM_EXEC_NODE_INSPECT"
        aria-label={`Inspect node ${node.id}, ${statusText}${evidenceDefect ? ", required evidence incomplete" : hasBlockingIssue ? `, ${blockingCount} blocking plan issue${blockingCount === 1 ? "" : "s"}` : ""}`}
        aria-current={selected ? "true" : undefined}
        title={`Inspect ${node.id}\nType: ${node.type}\nState: ${statusText}${evidenceDefect ? "\nEvidence gate: required output hash/artifact missing" : ""}${hasValidationIssue ? `\nPlan validation: ${validationIssues.map((issue) => issue.message).join("; ")}` : ""}\nGoal: ${node.node_goal}`}
        onClick={onSelect}
        onKeyDown={(event) => {
          if (event.key === "ArrowRight" || event.key === "ArrowDown") {
            event.preventDefault();
            onSelectAdjacent(1);
          }
          if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
            event.preventDefault();
            onSelectAdjacent(-1);
          }
        }}
        style={{
          width: `${nodeWidth}px`,
          minHeight: `${nodeHeight}px`,
          boxSizing: "border-box",
          borderRadius: 8,
          border: validationBorder,
          background: "var(--exec-card, #1c2230)",
          color: "var(--exec-text, #e5e7eb)",
          cursor: "pointer",
          display: "grid",
          gridTemplateColumns: "22px minmax(0, 1fr)",
          gridTemplateRows: "auto auto auto auto",
          alignItems: "center",
          columnGap: 8,
          rowGap: 3,
          padding: "10px 12px",
          textAlign: "left",
          font: "inherit",
        }}
      >
        {hasBlockingIssue || evidenceDefect ? <span className="exec-node-blocking-icon" aria-hidden>!</span> : <span aria-hidden style={{ gridRow: "1 / span 4", width: 14, height: 14, borderRadius: 999, background: stateColor[node.state] }} />}
        <span style={{ minWidth: 0, whiteSpace: "normal", overflowWrap: "anywhere", fontSize: 12, lineHeight: "15px", fontWeight: 700 }}>{node.id}</span>
        <span style={{ minWidth: 0, whiteSpace: "normal", overflowWrap: "anywhere", color: dimColor, fontSize: 11, lineHeight: "14px" }}>{node.type}</span>
        <span className={optional && node.state === "failed" ? "exec-node-status exec-node-status-warning" : "exec-node-status"}>{statusText}</span>
        <span className="exec-node-badge-row">
          {optional ? <span className="exec-node-optional-badge">Optional</span> : null}
          {evidenceDefect ? <span className="exec-node-validation-badge exec-node-validation-badge-blocking">Evidence gap</span> : null}
          {latestSemanticLabel ? <span className="exec-node-subagent-badge" title={`${semanticEventCount} semantic subagent event${semanticEventCount === 1 ? "" : "s"}`}>{latestSemanticLabel}</span> : null}
          {hasValidationIssue ? <span className={hasBlockingIssue ? "exec-node-validation-badge exec-node-validation-badge-blocking" : "exec-node-validation-badge"}>{hasBlockingIssue ? `${blockingCount} Block` : "Plan warning"}</span> : null}
        </span>
      </button>
    </foreignObject>
  );
}

function Inspector({
  node,
  state,
  result,
  runId,
  statusUpdatedAt,
  optional,
  onSelectNode,
  mode = "evidence",
  allNodes = [],
  validation,
  diff,
  runtimeReadinessNode,
  onUpdateNode,
  onAddDependency,
  onRemoveDependency,
  nodeEvents = [],
  availableModels,
  reviewCatalog,
  onSaveReviewCatalogEntry,
}: {
  node: ExecGraphNode;
  state: ExecNodeState;
  result?: Record<string, unknown>;
  runId?: string;
  statusUpdatedAt?: string;
  optional: boolean;
  onSelectNode: (nodeId: string) => void;
  mode?: DebuggerMode;
  allNodes?: ExecGraphNode[];
  validation?: PlanValidationResult;
  diff?: PlanDiffItem[];
  runtimeReadinessNode?: RuntimeReadinessNodeReport;
  onUpdateNode?: (fields: Partial<ExecGraphNode>) => void;
  onAddDependency?: (nodeId: string) => void;
  onRemoveDependency?: (nodeId: string) => void;
  nodeEvents?: ExecEvent[];
  availableModels?: string[];
  reviewCatalog?: ReviewCatalog;
  onSaveReviewCatalogEntry?: SaveReviewCatalogEntryHandler;
}) {
  useRegisterAction("scillm-exec-graph:plan-edit:goal", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_GOAL", label: "Edit node goal" });
  useRegisterAction("scillm-exec-graph:plan-edit:type", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_TYPE", label: "Edit node type" });
  useRegisterAction("scillm-exec-graph:plan-edit:role", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_ROLE", label: "Edit node role" });
  useRegisterAction("scillm-exec-graph:plan-edit:persona", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_PERSONA", label: "Edit node persona" });
  useRegisterAction("scillm-exec-graph:plan-edit:model", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_MODEL", label: "Edit node model" });
  useRegisterAction("scillm-exec-graph:plan-edit:goal-primary", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_GOAL_PRIMARY", label: "Edit selected node goal from top plan edit panel" });
  useRegisterAction("scillm-exec-graph:plan-edit:model-primary", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_MODEL_PRIMARY", label: "Edit selected node model from top plan edit panel" });
  useRegisterAction("scillm-exec-graph:plan-edit:prompt", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_PROMPT", label: "Edit node prompt" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT", label: "Edit review contract" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract-agent", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_AGENT", label: "Edit review contract agent" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract-model", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_MODEL", label: "Edit review contract model" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-level", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_LEVEL", label: "Edit review level" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-proof-level", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_PROOF_LEVEL", label: "Edit review proof level" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-read-only", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_READ_ONLY", label: "Edit review read-only flag" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract-prompt", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_PROMPT", label: "Edit review contract prompt" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract-preset", { app: "scillm", action: "SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_PRESET", label: "Edit review contract preset" });
  useRegisterAction("scillm-exec-graph:plan-edit:review-contract-duplicate", { app: "scillm", action: "SCILLM_EXEC_PLAN_DUPLICATE_REVIEW_SCOPE", label: "Duplicate review contract row" });
  useRegisterAction("scillm-exec-graph:plan-edit:dependency-select", { app: "scillm", action: "SCILLM_EXEC_PLAN_SELECT_DEPENDENCY", label: "Select dependency" });
  useRegisterAction("scillm-exec-graph:plan-edit:add-dependency", { app: "scillm", action: "SCILLM_EXEC_PLAN_ADD_DEPENDENCY", label: "Add dependency" });
  useRegisterAction("scillm-exec-graph:plan-edit:remove-dependency", { app: "scillm", action: "SCILLM_EXEC_PLAN_REMOVE_DEPENDENCY", label: "Remove dependency" });
  const [copied, setCopied] = useState(false);
  const [catalogSaveState, setCatalogSaveState] = useState<string>("");
  const [dependencyChoice, setDependencyChoice] = useState("");
  const [questionAnswers, setQuestionAnswers] = useState<Record<string, string>>({});
  const [committedAnswers, setCommittedAnswers] = useState<Record<string, string>>({});
  const promptPayload = JSON.stringify({ prompt: node.prompt, review_scopes: node.review_scopes, messages: node.messages, output_schema: node.output_schema }, null, 2);
  const hasPromptPayload = Boolean(node.prompt || node.review_scopes?.length || node.messages?.length || node.output_schema);
  const promptPayloadFields = [
    node.prompt ? "prompt" : "",
    node.review_scopes?.length ? `${node.review_scopes.length} review scope${node.review_scopes.length === 1 ? "" : "s"}` : "",
    node.messages?.length ? `${node.messages.length} message${node.messages.length === 1 ? "" : "s"}` : "",
    node.output_schema ? "output schema" : "",
  ].filter(Boolean).join(" · ");
  const promptPayloadHash = hasPromptPayload ? localIdentity(promptPayload) : "none";
  const promptRenderedAt = useMemo(() => new Date().toISOString(), [node.id, promptPayloadHash]);
  const dependencies = node.depends_on ?? [];
  const dependencyOptions = allNodes
    .filter((candidate) => candidate.id !== node.id && !dependencies.includes(candidate.id))
    .map((candidate) => {
      const cyclePath = cyclePathForDependency(node.id, candidate.id, allNodes);
      return { node: candidate, cyclePath };
    });
  const selectedDependencyOption = dependencyOptions.find((candidate) => candidate.node.id === dependencyChoice);
  const nodeValidationIssues = validation?.issues.filter((issue) => issue.node_id === node.id) ?? [];
  const nodeBlockingIssues = nodeValidationIssues.filter((issue) => issue.severity === "blocking");
  const nodeWarnings = nodeValidationIssues.filter((issue) => issue.severity === "warning");
  const nodeDiffItems = (diff ?? []).filter((item) => diffNodeIds(item).includes(node.id));
  const artifactValue = result?.artifact ?? result?.artifacts;
  const resultSource = result?.stdout_path ?? result?.final_json_path ?? result?.events_path ?? result?.stderr_path;
  const resultSourceLabel = resultSource ? String(resultSource).split("/").slice(-5).join("/") : "result source unavailable";
  const attemptValue = result?.attempt_id ?? result?.attempt;
  const outputHash = outputHashState(result?.output_hash, optional);
  const artifactLabelText = artifactLabel(artifactValue);
  const evidenceStatusText = evidenceStatus(result, optional);
  const requiredEvidenceIncomplete = !optional && state === "passed" && (outputHash.tone === "missing" || !artifactValue || evidenceStatusText === "Required evidence incomplete");
  const remediationText = requiredEvidenceIncomplete
    ? "Next action: rerun this node with hash-bound output evidence, or create a reviewed amendment that supplies the missing output artifact before phase closure."
    : mode === "evidence"
      ? "Next action: switch to Plan edit to propose a reviewed amendment; evidence mode is read-only."
      : "Next action: edit the draft node, validate the plan, then save an amendment record.";
  const modeContext = mode === "evidence" ? "Evidence node" : "Draft node";
  const dataContext = mode === "evidence" ? "Read-only evidence" : "Last run evidence below";
  const reviewScopes = node.review_scopes ?? [];
  const subagentEvents = nodeEvents.filter((event) => Boolean(event.event_type) || event.type.startsWith("subagent_") || event.type === "agent_event");
  const catalogAgents = reviewCatalogAgents(reviewCatalog);
  const catalogContracts = reviewCatalogContracts(reviewCatalog);
  const defaultCatalogContracts = reviewCatalogDefaultContractsForNode(node, reviewCatalog, allNodes ?? []);
  const topLevelModelChoices = modelChoices(availableModels, node.model);
  const scopeModelChoices = reviewScopeModelChoices(availableModels, reviewScopes);
  function updateReviewScope(index: number, fields: Partial<ReviewScopeSpec>) {
    const next = reviewScopes.map((scope, scopeIndex) => scopeIndex === index ? { ...scope, ...fields } : { ...scope });
    onUpdateNode?.({ review_scopes: next });
  }
  function addReviewScope(contractName = "correctness_regression") {
    const existing = new Set(reviewScopes.map(reviewContractName));
    const selectedContract = catalogContracts.map((contract) => contract.id).find((option) => !existing.has(option)) ?? contractName;
    onUpdateNode?.({
      review_scopes: [
        ...reviewScopes,
        defaultReviewScopeForContract(selectedContract, node, reviewCatalog),
      ],
    });
  }
  function addDefaultReviewScopes() {
    const existing = new Set(reviewScopes.map(reviewContractName));
    const additions = defaultCatalogContracts
      .filter((contract) => !existing.has(contract))
      .map((contract) => defaultReviewScopeForContract(contract, node, reviewCatalog));
    if (additions.length) onUpdateNode?.({ review_scopes: [...reviewScopes, ...additions] });
  }
  function setReviewScopePreset(index: number, preset: string) {
    const scope = reviewScopes[index];
    if (!scope) return;
    const contract = reviewContractName(scope);
    const priorOverrides = scope.inline_overrides ?? {};
    updateReviewScope(index, {
      prompt_preset: preset,
      prompt: preset === "custom" ? scope.prompt : defaultReviewContractPrompt(contract, preset, reviewCatalog),
      inline_overrides: preset === "custom" ? { ...priorOverrides, prompt: true } : {},
    });
  }
  function duplicateReviewScope(index: number) {
    const scope = reviewScopes[index];
    if (!scope) return;
    const baseContract = reviewContractName(scope);
    const existing = new Set(reviewScopes.map(reviewContractName));
    const duplicateContract = `${baseContract || "custom_contract"}_copy`;
    const nextContract = existing.has(duplicateContract) ? `${duplicateContract}_${reviewScopes.length + 1}` : duplicateContract;
    const duplicate = {
      ...scope,
      scope: nextContract,
      contract: nextContract,
      prompt_preset: "custom",
      enabled: true,
    };
    onUpdateNode?.({ review_scopes: [...reviewScopes.slice(0, index + 1), duplicate, ...reviewScopes.slice(index + 1)] });
  }
  async function saveReviewContract(index: number) {
    const scope = reviewScopes[index];
    if (!scope || !onSaveReviewCatalogEntry) return;
    const contract = reviewContractName(scope);
    if (!contract) return;
    setCatalogSaveState(`Saving ${contract}`);
    try {
      await onSaveReviewCatalogEntry("contracts", {
        id: contract,
        version: scope.catalog_version ?? reviewContractEntry(contract, reviewCatalog)?.version ?? "1",
        label: reviewContractEntry(contract, reviewCatalog)?.label ?? titleCase(contract),
        default_agent: scope.agent ?? defaultReviewAgentForContract(contract, reviewCatalog),
        default_model: scope.model ?? node.model ?? defaultReviewModelForContract(contract, reviewCatalog),
        default_preset: scope.prompt_preset ?? "custom",
        review_level: scope.review_level ?? reviewContractEntry(contract, reviewCatalog)?.review_level ?? "default",
        proof_level: scope.proof_level ?? reviewContractEntry(contract, reviewCatalog)?.proof_level ?? "static_confirmed",
        reducer_policy: scope.reducer_policy ?? reviewContractEntry(contract, reviewCatalog)?.reducer_policy ?? "evidence_backed_only",
        read_only: scope.read_only ?? true,
        evidence_required: scope.evidence_required ?? true,
        closure_authority: scope.closure_authority ?? "final_review_gate",
        risk_triggers: scope.risk_triggers ?? reviewContractEntry(contract, reviewCatalog)?.risk_triggers,
        best_practice_skills: scope.best_practice_skills?.length ? scope.best_practice_skills : defaultBestPracticeSkillsForContract(contract, reviewCatalog),
        compatible_node_types: reviewContractEntry(contract, reviewCatalog)?.compatible_node_types ?? ["review-code"],
        required_fields: reviewContractEntry(contract, reviewCatalog)?.required_fields ?? ["agent", "model", "contract", "proof_level", "best_practice_skills"],
        default: reviewCatalogDefaultContracts(reviewCatalog).includes(contract),
        prompt: scope.prompt ?? "",
      });
      setCatalogSaveState(`Saved ${contract}`);
      window.setTimeout(() => setCatalogSaveState(""), 1600);
    } catch (error) {
      setCatalogSaveState(error instanceof Error ? error.message : String(error));
    }
  }
  async function saveReviewAgent(index: number) {
    const scope = reviewScopes[index];
    if (!scope?.agent || !onSaveReviewCatalogEntry) return;
    const agent = scope.agent;
    setCatalogSaveState(`Saving ${agent}`);
    try {
      await onSaveReviewCatalogEntry("agents", {
        id: agent,
        version: reviewCatalogAgents(reviewCatalog).find((entry) => entry.id === agent)?.version ?? "1",
        label: reviewCatalogAgents(reviewCatalog).find((entry) => entry.id === agent)?.label ?? titleCase(agent),
        default_model: scope.model ?? node.model ?? "oc-kimi",
        compatible_node_types: reviewCatalogAgents(reviewCatalog).find((entry) => entry.id === agent)?.compatible_node_types ?? ["review-code"],
        read_only: scope.read_only ?? true,
        evidence_required: scope.evidence_required ?? true,
        prompt: reviewCatalogAgents(reviewCatalog).find((entry) => entry.id === agent)?.prompt ?? "Stay read-only. Ground every finding in concrete file, diff, test, log, command, or artifact evidence.",
      });
      setCatalogSaveState(`Saved ${agent}`);
      window.setTimeout(() => setCatalogSaveState(""), 1600);
    } catch (error) {
      setCatalogSaveState(error instanceof Error ? error.message : String(error));
    }
  }
  function removeReviewScope(index: number) {
    onUpdateNode?.({ review_scopes: reviewScopes.filter((_, scopeIndex) => scopeIndex !== index) });
  }
  useEffect(() => {
    setDependencyChoice("");
  }, [node.id]);
  useEffect(() => {
    setQuestionAnswers({});
    setCommittedAnswers({});
  }, [node.id]);
  async function copyPromptPayload() {
    setCopied(true);
    try {
      await navigator.clipboard?.writeText(promptPayload);
    } catch {
      // Headless browsers and locked-down contexts can deny clipboard writes;
      // the click still needs immediate visible feedback.
    }
    window.setTimeout(() => setCopied(false), 1400);
  }

  return (
    <div style={{ padding: 16, display: "grid", gap: 16 }}>
      <div className="exec-inspector-header exec-inspector-sticky-summary">
        <div style={{ color: dimColor, fontSize: 12, textTransform: "uppercase", letterSpacing: 0 }}>Node frame</div>
        <h3 style={{ margin: "4px 0", fontSize: 18 }}>Selected node: {node.id}</h3>
        <div className="exec-mode-badge-row" aria-label="Inspector mode and data source">
          <span className={mode === "evidence" ? "exec-readonly-node-badge" : "exec-draft-node-badge"}>{modeContext}</span>
          <span className="exec-source-node-badge">{dataContext}</span>
        </div>
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 12 }}><span aria-hidden style={{ width: 10, height: 10, borderRadius: 999, background: stateColor[state] }} />{optional && state === "failed" ? "Optional failed" : stateLabel[state]} · {node.type}</span>
        <div className={requiredEvidenceIncomplete ? "exec-inspector-proof-summary exec-inspector-proof-summary-blocked" : "exec-inspector-proof-summary"}>{optional && state === "failed" ? "Optional failure - Non-blocking - REQUIRED = no - Evidence absence allowed" : requiredEvidenceIncomplete ? "Evidence incomplete - Closure blocked - Required node" : `${stateLabel[state]} - ${optional ? "Optional node" : "Required node"} - Inspector selection`}</div>
        <div className="exec-compliance-summary-heading">Compliance summary</div>
        <div className="exec-node-summary-grid">
          <Info label="Impact" value={nodeImpactText(optional, state, requiredEvidenceIncomplete)} />
          <Info label="Run provenance" value={[runId ? `run ${runId}` : "run id unavailable", attemptValue ? `attempt ${String(attemptValue)}` : "attempt unavailable"].join(" · ")} />
          <Info label="Status freshness" value={statusUpdatedAt ? formatTimestamp(statusUpdatedAt) : "status timestamp unavailable"} />
          <Info label="Result source" value={resultSourceLabel} />
          <Info label="Required" value={optional ? "No" : "Yes"} />
          <Info label={artifactLabelText} value={String(artifactValue ?? "Not reported")} />
          <Info label="Output hash" value={outputHash.text}><EvidenceBadge tone={outputHash.tone} text={outputHash.text} /></Info>
          <Info label="Evidence status" value={evidenceStatusText}><EvidenceBadge tone={optional ? "optional" : outputHash.tone} text={evidenceStatusText} /></Info>
          <Info label="Plan issues" value={`${nodeBlockingIssues.length} blocking, ${nodeWarnings.length} warnings`}>
            <span className={nodeBlockingIssues.length ? "exec-compliance-issue-badge exec-compliance-issue-badge-blocking" : nodeWarnings.length ? "exec-compliance-issue-badge exec-compliance-issue-badge-warning" : "exec-compliance-issue-badge"}>
              {nodeBlockingIssues.length ? `${nodeBlockingIssues.length} blocking` : nodeWarnings.length ? `${nodeWarnings.length} warning${nodeWarnings.length === 1 ? "" : "s"}` : "No node issues"}
            </span>
          </Info>
          <Info label="Draft diff" value={nodeDiffItems.length ? nodeDiffItems.map((item) => diffParticipationSummary(item, node.id)).join("; ") : "Not in current draft diff"}>
            {nodeDiffItems.length ? (
              <span className="exec-compliance-issue-badge">Participates in draft diff: {diffParticipationSummary(nodeDiffItems[0], node.id)}</span>
            ) : (
              <span className="exec-compliance-issue-badge">Not in current draft diff</span>
            )}
          </Info>
        </div>
        <div className={requiredEvidenceIncomplete ? "exec-remediation-callout exec-remediation-callout-blocking" : "exec-remediation-callout"} data-qid="scillm-exec-graph:selected-node:next-action">
          {remediationText}
        </div>
      </div>
      {mode === "plan_edit" ? (
        <Section title="Plan edit focus">
          <label className="exec-plan-field">
            <span>Goal</span>
            <textarea className="exec-plan-input exec-plan-textarea" data-qid="scillm-exec-graph:plan-edit:goal-primary" data-qs-action="SCILLM_EXEC_PLAN_EDIT_GOAL_PRIMARY" title="Edit selected node goal" value={node.node_goal} onChange={(event) => onUpdateNode?.({ node_goal: event.target.value })} />
          </label>
          <div className="exec-plan-sensitive-group">
            <div className="exec-plan-subheading">AI model and compliance contract</div>
            <label className="exec-plan-field">
              <span>Model</span>
              <select className="exec-plan-input" data-qid="scillm-exec-graph:plan-edit:model-primary" data-qs-action="SCILLM_EXEC_PLAN_EDIT_MODEL_PRIMARY" title="Select selected node model" value={node.model ?? ""} onChange={(event) => onUpdateNode?.({ model: event.target.value || undefined })}>
                {topLevelModelChoices.map((option) => <option key={option || "default"} value={option}>{option || "default"}</option>)}
              </select>
            </label>
          </div>
        </Section>
      ) : null}
      <Section title="Contract"><Info label="Goal" value={node.node_goal} /><Info label="Role" value={node.protocol_role ?? "worker"} /><Info label="Persona" value={node.persona_ref ?? "none"} /><Info label="Required" value={optional ? "no, optional node" : "yes"} /><Info label="Failure classification" value={requiredEvidenceIncomplete ? "Evidence incomplete" : optional && state === "failed" ? "Optional failure" : state === "failed" ? "Required failure" : "No failure"} /><Info label="Impact on run result" value={nodeImpactText(optional, state, requiredEvidenceIncomplete)} /></Section>
      <Section title="Human collaboration">
        <Info label="Interaction model" value="Interview-style task contract" />
        <Info label="Recommendation" value={node.recommendation ?? "No recommendation declared"} />
        <Info label="Reason" value={node.reason ?? "No rationale declared"} />
        {node.human_questions?.length ? (
          <div className="exec-interview-question-list" data-qid="scillm-exec-graph:interview-questions">
            {node.human_questions.map((question, index) => (
              <div className="exec-interview-question" key={question.id || index}>
                <div className="exec-interview-question-heading">
                  <span>{question.header ?? `Question ${index + 1}`}</span>
                  <code>{question.multi_select ? "multi-select" : "single-select"}</code>
                </div>
                <p>{question.text}</p>
                {question.recommendation ? <div className="exec-interview-recommendation"><b>Recommended</b>{question.recommendation}{question.reason ? ` - ${question.reason}` : ""}</div> : null}
                {question.options?.length ? (
                  <div className="exec-interview-options" role={question.multi_select ? "group" : "radiogroup"} aria-label={question.header ?? `Question ${index + 1}`}>
                    {question.options.map((option, optionIndex) => (
                      <label className="exec-interview-option" key={`${question.id || index}-${optionIndex}`}>
                        <input
                          type={question.multi_select ? "checkbox" : "radio"}
                          name={`interview-${question.id || index}`}
                          value={optionLabel(option)}
                          checked={(questionAnswers[question.id || String(index)] ?? "") === optionLabel(option)}
                          onChange={(event) => {
                            if (question.multi_select) {
                              setQuestionAnswers((answers) => ({ ...answers, [question.id || String(index)]: event.currentTarget.checked ? optionLabel(option) : "" }));
                            } else {
                              setQuestionAnswers((answers) => ({ ...answers, [question.id || String(index)]: optionLabel(option) }));
                            }
                          }}
                        />
                        <span>{optionLabel(option)}</span>
                        {optionDescription(option) ? <small>{optionDescription(option)}</small> : null}
                      </label>
                    ))}
                    <label className="exec-interview-option">
                      <input
                        type={question.multi_select ? "checkbox" : "radio"}
                        name={`interview-${question.id || index}`}
                        value="Other"
                        checked={(questionAnswers[question.id || String(index)] ?? "") === "Other"}
                        onChange={() => setQuestionAnswers((answers) => ({ ...answers, [question.id || String(index)]: "Other" }))}
                      />
                      <span>Other</span>
                      <small>Custom text or image input when this node needs human clarification.</small>
                    </label>
                  </div>
                ) : <div className="exec-empty-state">No explicit options. Use this node as a free-text clarification point.</div>}
                <div className="exec-interview-commit-row">
                  <button
                    type="button"
                    className="exec-control-button exec-control-button-compact"
                    disabled={!questionAnswers[question.id || String(index)]}
                    aria-disabled={!questionAnswers[question.id || String(index)]}
                    onClick={() => setCommittedAnswers((answers) => ({ ...answers, [question.id || String(index)]: questionAnswers[question.id || String(index)] }))}
                  >
                    Commit answer
                  </button>
                  {committedAnswers[question.id || String(index)] ? <span>Committed: {committedAnswers[question.id || String(index)]}</span> : <span>Select an option to record the human decision.</span>}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="exec-empty-state" data-qid="scillm-exec-graph:interview-questions-empty">No human questions declared for this node. Add questions when this workflow needs a structured human decision instead of raw JSON edits.</div>
        )}
      </Section>
      <Section title="Subagent activity">
        {subagentEvents.length ? (
          <div className="exec-subagent-event-list" data-qid="scillm-exec-graph:subagent-events">
            {subagentEvents.slice(-8).reverse().map((event, index) => (
              <div className="exec-subagent-event" key={`${event.ts ?? "event"}-${index}`}>
                <div className="exec-subagent-event-heading">
                  <span>{event.event_type ?? event.type}</span>
                  <code>{event.actor ?? "system"}</code>
                </div>
                <p>{event.text ?? event.type}</p>
                <small>{event.ts ? formatTimestamp(event.ts) : "timestamp unavailable"}</small>
              </div>
            ))}
          </div>
        ) : (
          <div className="exec-empty-state" data-qid="scillm-exec-graph:subagent-events-empty">No subagent events recorded for this node. The viewer only shows runtime subagent state when the event stream provides node-bound semantic events.</div>
        )}
      </Section>
      <Section title="Runtime">
        <Info label="Type" value={node.type} />
        <Info label="Model" value={node.model ?? "default"} />
        <Info label="Depends on" value={dependencies.length ? dependencies.join(", ") : "none"}>
          {dependencies.length ? (
            <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {dependencies.map((dependency) => (
                <button key={dependency} className="exec-link-button" type="button" onClick={() => onSelectNode(dependency)} title={`Select dependency ${dependency}`}>{dependency}</button>
              ))}
            </span>
          ) : null}
        </Info>
      </Section>
      {mode === "plan_edit" ? (
        <Section title="Plan edit">
          <label className="exec-plan-field">
            <span>Goal</span>
            <textarea className="exec-plan-input exec-plan-textarea" data-qid="scillm-exec-graph:plan-edit:goal" data-qs-action="SCILLM_EXEC_PLAN_EDIT_GOAL" title="Edit selected node goal" value={node.node_goal} onChange={(event) => onUpdateNode?.({ node_goal: event.target.value })} />
          </label>
          <label className="exec-plan-field">
            <span>Type</span>
            <input className="exec-plan-input" data-qid="scillm-exec-graph:plan-edit:type" data-qs-action="SCILLM_EXEC_PLAN_EDIT_TYPE" title="Edit selected node type" value={node.type} onChange={(event) => onUpdateNode?.({ type: event.target.value })} />
          </label>
          <div className="exec-plan-sensitive-group">
            <div className="exec-plan-subheading">AI model and compliance contract</div>
            <label className="exec-plan-field">
              <span>Role</span>
              <select className="exec-plan-input" data-qid="scillm-exec-graph:plan-edit:role" data-qs-action="SCILLM_EXEC_PLAN_EDIT_ROLE" title="Select verified protocol role" value={node.protocol_role ?? ""} onChange={(event) => onUpdateNode?.({ protocol_role: event.target.value })}>
                <option value="">worker</option>
                <option value="worker">worker</option>
                <option value="reviewer">reviewer</option>
                <option value="verifier">verifier</option>
                <option value="planner">planner</option>
                <option value="tool">tool</option>
              </select>
              <span className="exec-plan-inline-help">Verified role controls how this node is reviewed and gated.</span>
            </label>
            <label className="exec-plan-field">
              <span>Persona</span>
              <select className="exec-plan-input" data-qid="scillm-exec-graph:plan-edit:persona" data-qs-action="SCILLM_EXEC_PLAN_EDIT_PERSONA" title="Select review persona" value={node.persona_ref ?? ""} onChange={(event) => onUpdateNode?.({ persona_ref: event.target.value })}>
                <option value="">none</option>
                <option value="nico-bailon">nico-bailon</option>
                <option value="margaret-chen">margaret-chen</option>
                <option value="brandon-bailey">brandon-bailey</option>
                <option value="rob-armstrong">rob-armstrong</option>
              </select>
              <span className="exec-plan-inline-help">Persona selection affects compliance and design review posture.</span>
            </label>
            <label className="exec-plan-field">
              <span>Model</span>
              <select className="exec-plan-input" data-qid="scillm-exec-graph:plan-edit:model" data-qs-action="SCILLM_EXEC_PLAN_EDIT_MODEL" title="Select selected node model" value={node.model ?? ""} onChange={(event) => onUpdateNode?.({ model: event.target.value || undefined })}>
                {topLevelModelChoices.map((option) => <option key={option || "default"} value={option}>{option || "default"}</option>)}
              </select>
            </label>
            {isReviewCodeNode(node) ? (
              <div className="exec-review-scope-editor" data-qid="scillm-exec-graph:plan-edit:review-scopes">
                <div className="exec-plan-panel-heading exec-plan-panel-heading-row">
                  <span>review-code fanout</span>
                  <span className="exec-review-scope-toolbar">
                    <button
                      className="exec-control-button exec-control-button-compact"
                      type="button"
                      data-qid="scillm-exec-graph:plan-edit:add-default-review-scopes"
                      data-qs-action="SCILLM_EXEC_PLAN_ADD_DEFAULT_REVIEW_SCOPES"
                      title="Add default review-code fanout contracts"
                      onClick={() => addDefaultReviewScopes()}
                    >
                      Add defaults
                    </button>
                    <button
                      className="exec-control-button exec-control-button-compact"
                      type="button"
                      data-qid="scillm-exec-graph:plan-edit:add-review-scope"
                      data-qs-action="SCILLM_EXEC_PLAN_ADD_REVIEW_SCOPE"
                      title="Add one review-code fanout contract"
                      onClick={() => addReviewScope()}
                    >
                      Add contract
                    </button>
                  </span>
                </div>
                {reviewScopes.length ? (
                  <div className="exec-review-scope-list">
                    {reviewScopes.map((scope, index) => {
                      const contract = reviewContractName(scope);
                      const bestPracticeSkills = scope.best_practice_skills ?? defaultBestPracticeSkillsForContract(contract, reviewCatalog);
                      return (
                      <div key={`${contract || "contract"}-${index}`} className="exec-review-scope-row" data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}`}>
                        <label className="exec-plan-field">
                          <span>Agent</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:agent`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_AGENT"
                            title="Select reviewer agent for this contract"
                            value={scope.agent ?? defaultReviewAgentForContract(contract, reviewCatalog)}
                            onChange={(event) => updateReviewScope(index, { agent: event.target.value })}
                          >
                            {scope.agent && !catalogAgents.some((option) => option.id === scope.agent) ? <option value={scope.agent}>{scope.agent} (draft)</option> : null}
                            {catalogAgents.map((option) => <option key={option.id} value={option.id}>{option.label ?? option.id}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field">
                          <span>Contract</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:contract`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT"
                            title="Select evidence contract for this fanout row"
                            value={contract}
                            onChange={(event) => updateReviewScope(index, {
                              scope: event.target.value,
                              contract: event.target.value,
                              ...catalogIdentityFields(reviewContractEntry(event.target.value, reviewCatalog)),
                              agent: defaultReviewAgentForContract(event.target.value, reviewCatalog),
                              model: node.model || defaultReviewModelForContract(event.target.value, reviewCatalog),
                              review_level: reviewContractEntry(event.target.value, reviewCatalog)?.review_level ?? (event.target.value === "security" ? "risk_expanded" : "default"),
                              proof_level: reviewContractEntry(event.target.value, reviewCatalog)?.proof_level ?? (event.target.value === "tests_validation" || event.target.value === "evidence_closure_safety" ? "proven" : "static_confirmed"),
                              reducer_policy: reviewContractEntry(event.target.value, reviewCatalog)?.reducer_policy ?? (event.target.value === "evidence_closure_safety" ? "fail_closed_evidence_closure" : "evidence_backed_only"),
                              read_only: reviewContractEntry(event.target.value, reviewCatalog)?.read_only ?? true,
                              evidence_required: reviewContractEntry(event.target.value, reviewCatalog)?.evidence_required ?? true,
                              closure_authority: reviewContractEntry(event.target.value, reviewCatalog)?.closure_authority ?? "final_review_gate",
                              risk_triggers: reviewContractEntry(event.target.value, reviewCatalog)?.risk_triggers,
                              best_practice_skills: defaultBestPracticeSkillsForContract(event.target.value, reviewCatalog),
                              prompt: defaultReviewContractPrompt(event.target.value, reviewContractEntry(event.target.value, reviewCatalog)?.default_preset ?? "scope_default", reviewCatalog),
                              prompt_preset: reviewContractEntry(event.target.value, reviewCatalog)?.default_preset ?? "scope_default",
                              inline_overrides: {},
                            })}
                          >
                            {contract && !catalogContracts.some((option) => option.id === contract) ? <option value={contract}>{contract} (draft)</option> : null}
                            {catalogContracts.map((option) => <option key={option.id} value={option.id}>{option.label ?? option.id}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field">
                          <span>Model</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:model`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_MODEL"
                            title="Select scillm model alias for this contract"
                            value={scope.model ?? node.model ?? "oc-kimi"}
                            onChange={(event) => updateReviewScope(index, { model: event.target.value })}
                          >
                            {scopeModelChoices.filter(Boolean).map((option) => <option key={option} value={option}>{option}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field">
                          <span>Review level</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:review-level`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_LEVEL"
                            title="Select review expansion level for this evidence contract"
                            value={scope.review_level ?? reviewContractEntry(contract, reviewCatalog)?.review_level ?? "default"}
                            onChange={(event) => updateReviewScope(index, { review_level: event.target.value })}
                          >
                            {reviewLevelOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field">
                          <span>Proof floor</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:proof-level`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_PROOF_LEVEL"
                            title="Select the minimum proof level accepted by the reducer"
                            value={scope.proof_level ?? reviewContractEntry(contract, reviewCatalog)?.proof_level ?? "static_confirmed"}
                            onChange={(event) => updateReviewScope(index, { proof_level: event.target.value })}
                          >
                            {proofLevelOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field">
                          <span>Contract preset</span>
                          <select
                            className="exec-plan-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:prompt-preset`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_PRESET"
                            title="Select prompt preset for this evidence contract"
                            value={scope.prompt_preset ?? "custom"}
                            onChange={(event) => setReviewScopePreset(index, event.target.value)}
                          >
                            {reviewCodePromptPresetOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                          </select>
                        </label>
                        <label className="exec-plan-field exec-review-scope-best-practices">
                          <span>Best-practice skills</span>
                          <textarea
                            className="exec-plan-input exec-plan-textarea exec-review-scope-best-practices-input"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:best-practice-skills`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_BEST_PRACTICES"
                            title="Comma- or newline-separated best-practices-* skills that must be loaded before this fanout reviewer runs"
                            value={formatBestPracticeSkills(bestPracticeSkills)}
                            onChange={(event) => updateReviewScope(index, { best_practice_skills: parseBestPracticeSkills(event.target.value) })}
                          />
                          {bestPracticeSkills.length ? null : <span className="exec-plan-inline-warning">best-practices-* skills are required for this fanout row.</span>}
                        </label>
                        <label className="exec-plan-field exec-review-scope-enabled">
                          <span>Enabled</span>
                          <input
                            type="checkbox"
                            checked={scope.enabled !== false}
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:enabled`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_SCOPE_ENABLED"
                            title="Enable this fanout review call"
                            onChange={(event) => updateReviewScope(index, { enabled: event.target.checked })}
                          />
                        </label>
                        <label className="exec-plan-field exec-review-scope-enabled">
                          <span>Read-only</span>
                          <input
                            type="checkbox"
                            checked={scope.read_only !== false}
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:read-only`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_READ_ONLY"
                            title="Review fanout calls must stay read-only by default"
                            onChange={(event) => updateReviewScope(index, { read_only: event.target.checked })}
                          />
                        </label>
                        <label className="exec-plan-field exec-review-scope-prompt">
                          <span>Prompt contract</span>
                          <textarea
                            className="exec-plan-input exec-plan-textarea"
                            data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:prompt`}
                            data-qs-action="SCILLM_EXEC_PLAN_EDIT_REVIEW_CONTRACT_PROMPT"
                            title="Edit this evidence contract prompt body"
                            value={scope.prompt ?? ""}
                            onChange={(event) => updateReviewScope(index, { prompt: event.target.value, prompt_preset: "custom", inline_overrides: { ...(scope.inline_overrides ?? {}), prompt: true } })}
                          />
                        </label>
                        <button
                          className="exec-control-button exec-control-button-compact exec-review-scope-remove"
                          type="button"
                          data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:save-contract`}
                          data-qs-action="SCILLM_EXEC_PLAN_SAVE_REVIEW_CONTRACT"
                          title={onSaveReviewCatalogEntry ? `Save review contract ${contract || index + 1} to the catalog` : "No review catalog save backend connected"}
                          disabled={!onSaveReviewCatalogEntry || !contract}
                          onClick={() => void saveReviewContract(index)}
                        >
                          Save contract
                        </button>
                        <button
                          className="exec-control-button exec-control-button-compact exec-review-scope-remove"
                          type="button"
                          data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:save-agent`}
                          data-qs-action="SCILLM_EXEC_PLAN_SAVE_REVIEW_AGENT"
                          title={onSaveReviewCatalogEntry ? `Save review agent ${scope.agent || index + 1} to the catalog` : "No review catalog save backend connected"}
                          disabled={!onSaveReviewCatalogEntry || !scope.agent}
                          onClick={() => void saveReviewAgent(index)}
                        >
                          Save agent
                        </button>
                        <button
                          className="exec-control-button exec-control-button-compact exec-review-scope-remove"
                          type="button"
                          data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:duplicate`}
                          data-qs-action="SCILLM_EXEC_PLAN_DUPLICATE_REVIEW_SCOPE"
                          title={`Duplicate review contract ${contract || index + 1}`}
                          onClick={() => duplicateReviewScope(index)}
                        >
                          Duplicate
                        </button>
                        <button
                          className="exec-control-button exec-control-button-compact exec-review-scope-remove"
                          type="button"
                          data-qid={`scillm-exec-graph:plan-edit:review-scope:${index}:remove`}
                          data-qs-action="SCILLM_EXEC_PLAN_REMOVE_REVIEW_SCOPE"
                          title={`Remove review contract ${contract || index + 1}`}
                          onClick={() => removeReviewScope(index)}
                        >
                          Remove
                        </button>
                      </div>
                    )})}
                  </div>
                ) : <div className="exec-empty-state">Agent has not selected review fanout contracts for this review-code node.</div>}
                {catalogSaveState ? <div className="exec-plan-inline-help">{catalogSaveState}</div> : null}
                <div className="exec-plan-inline-help">Enabled fanout rows require agent, evidence contract, model, proof floor, and best-practices-* skills. Agents/contracts load from /v1/scillm/exec/review-catalog; review-safe models load from /v1/scillm/models review_fanout_models/selectable_models. Save contract or agent updates the catalog; Save amendment persists the DAG edit.</div>
              </div>
            ) : null}
            <div className="exec-plan-obligation-summary" aria-label="Verification gates for this draft node">
              <span><b>Obligation status</b>Draft contract under review</span>
              <span><b>Gate</b>Plan validation plus reviewer evidence</span>
              <span><b>Persistence</b>Memory amendment record after save</span>
            </div>
            <label className="exec-plan-field">
              <span>Prompt</span>
              <textarea className="exec-plan-input exec-plan-textarea" data-qid="scillm-exec-graph:plan-edit:prompt" data-qs-action="SCILLM_EXEC_PLAN_EDIT_PROMPT" title="Edit selected node prompt contract" value={node.prompt ?? ""} onChange={(event) => onUpdateNode?.({ prompt: event.target.value })} />
              {nodeValidationIssues.some((issue) => issue.code === "missing_prompt_contract") ? <span className="exec-plan-inline-warning">Prompt-like node is missing a prompt or messages.</span> : null}
            </label>
          </div>
          <div className="exec-plan-dependencies" data-qid="scillm-exec-graph:plan-edit:dependencies">
            <div className="exec-info-label">Dependencies</div>
            <div className="exec-plan-inline-help">Keyboard: focus the selector, choose a node, then press Enter or Space on Add dependency.</div>
            {dependencies.length ? (
              <div className="exec-plan-list">
                {dependencies.map((dependency) => (
                  <span key={dependency} className="exec-plan-dependency-pill">
                    <button className="exec-link-button" type="button" onClick={() => onSelectNode(dependency)}>{dependency}</button>
                    <button className="exec-plan-remove-button" type="button" data-qid={`scillm-exec-graph:plan-edit:remove-dependency:${dependency}`} data-qs-action="SCILLM_EXEC_PLAN_REMOVE_DEPENDENCY" title={`Remove dependency ${dependency}`} onClick={() => onRemoveDependency?.(dependency)}>Remove</button>
                  </span>
                ))}
              </div>
            ) : <div className="exec-empty-state">No dependencies. Use the options below to add one.</div>}
            <div className="exec-plan-add-dependency">
              <select className="exec-plan-input" value={dependencyChoice} data-qid="scillm-exec-graph:plan-edit:dependency-select" data-qs-action="SCILLM_EXEC_PLAN_SELECT_DEPENDENCY" title="Select dependency to add" onChange={(event) => setDependencyChoice(event.target.value)}>
                <option value="">Select node</option>
                {dependencyOptions.map((candidate) => <option key={candidate.node.id} value={candidate.node.id} disabled={Boolean(candidate.cyclePath)}>{candidate.node.id}{candidate.cyclePath ? " (would create cycle)" : ""}</option>)}
              </select>
              <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:add-dependency" data-qs-action="SCILLM_EXEC_PLAN_ADD_DEPENDENCY" title={selectedDependencyOption?.cyclePath ? `Would create cycle: ${selectedDependencyOption.cyclePath}` : dependencyChoice ? "Add selected dependency to draft; keyboard Enter or Space activates this button" : "Select a dependency first"} disabled={!dependencyChoice || Boolean(selectedDependencyOption?.cyclePath)} aria-disabled={!dependencyChoice || Boolean(selectedDependencyOption?.cyclePath)} onClick={() => {
                if (!dependencyChoice || selectedDependencyOption?.cyclePath) return;
                onAddDependency?.(dependencyChoice);
                setDependencyChoice("");
              }}>Add dependency</button>
            </div>
            {dependencyOptions.length ? (
              <div className="exec-plan-available-dependencies" aria-label="Available dependencies">
                {dependencyOptions.map((candidate) => (
                  <button
                    key={candidate.node.id}
                    className="exec-control-button exec-control-button-compact"
                    type="button"
                    data-qid={`scillm-exec-graph:plan-edit:add-dependency:${candidate.node.id}`}
                    data-qs-action="SCILLM_EXEC_PLAN_ADD_DEPENDENCY"
                    title={candidate.cyclePath ? `Would create cycle: ${candidate.cyclePath}` : `Add dependency ${candidate.node.id}`}
                    disabled={Boolean(candidate.cyclePath)}
                    aria-disabled={Boolean(candidate.cyclePath)}
                    onClick={() => {
                      if (candidate.cyclePath) return;
                      onAddDependency?.(candidate.node.id);
                    }}
                  >
                    {candidate.cyclePath ? `Blocked ${candidate.node.id}` : `Add ${candidate.node.id}`}
                  </button>
                ))}
              </div>
            ) : null}
            {dependencyOptions.some((candidate) => candidate.cyclePath) ? (
              <div className="exec-plan-invalid-dependencies">
                {dependencyOptions.filter((candidate) => candidate.cyclePath).map((candidate) => (
                  <span key={candidate.node.id}>Would create cycle: {candidate.cyclePath}</span>
                ))}
              </div>
            ) : null}
          </div>
          {nodeValidationIssues.length ? <ValidationIssueList issues={nodeValidationIssues} onSelectNode={onSelectNode} /> : <div className="exec-plan-issue exec-plan-issue-info">No node issues.</div>}
        </Section>
      ) : null}
      {mode === "plan_edit" && runtimeReadinessNode ? (
        <Section title="Plan-iterate readiness">
          <Info label="Adapter" value={runtimeReadinessNode.adapter} />
          <Info label="Status" value={runtimeReadinessNode.status.replaceAll("_", " ")} />
          <Info label="Missing fields" value={runtimeReadinessNode.missing_fields.length ? runtimeReadinessNode.missing_fields.join(", ") : "none"}>
            {runtimeReadinessNode.missing_fields.length ? (
              <span className="exec-readiness-field-list">
                {runtimeReadinessNode.missing_fields.map((field) => <span key={field} className="exec-readiness-field">{field}</span>)}
              </span>
            ) : <span className="exec-compliance-issue-badge">No missing fields</span>}
          </Info>
          <Info label="Present fields" value={runtimeReadinessNode.present_fields.length ? runtimeReadinessNode.present_fields.join(", ") : "none"} />
          <Info label="Next action" value={runtimeReadinessNode.next_action} />
          {runtimeReadinessNode.inferred_fields.length ? (
            <Info label="Inferred fields" value={runtimeReadinessNode.inferred_fields.map((field) => `${field.field} from ${field.source}`).join("; ")} />
          ) : null}
        </Section>
      ) : null}
      <Section title={mode === "evidence" ? "Execution evidence" : "Last run evidence"}>
        <div className="exec-evidence-note">Node execution timestamps are UTC.</div>
        <Info label="Node ID" value={node.id} />
        <EvidenceInfo label="Attempt" value={result?.attempt_id ?? result?.attempt} node={node} optional={optional} />
        <EvidenceInfo label="Started at UTC" value={result?.started_at ?? result?.start_time} node={node} optional={optional} />
        <EvidenceInfo label="Completed at UTC" value={result?.completed_at ?? result?.end_time} node={node} optional={optional} />
        <EvidenceInfo label="Duration" value={result?.duration_ms ? `${result.duration_ms} ms` : undefined} node={node} optional={optional} />
        <EvidenceInfo label={artifactLabelText} value={artifactValue} node={node} optional={optional} />
        <Info label="Output hash" value={outputHash.text}>
          <EvidenceBadge tone={outputHash.tone} text={outputHash.text} />
          {outputHash.note ? <div className="exec-evidence-note">{outputHash.note}</div> : null}
        </Info>
        <Info label="Evidence status" value={evidenceStatusText}><EvidenceBadge tone={optional ? "optional" : outputHash.tone} text={evidenceStatusText} /></Info>
      </Section>
      <Section
        title="Prompt payload"
        action={<button className="exec-control-button exec-control-button-compact" data-qid="scillm-exec-graph:prompt-payload:copy" data-qs-action="SCILLM_EXEC_PROMPT_PAYLOAD_COPY" title={hasPromptPayload ? "Copy prompt payload JSON" : "No prompt payload to copy"} disabled={!hasPromptPayload} aria-disabled={!hasPromptPayload} onClick={() => void copyPromptPayload()}>{copied ? "Copied" : "Copy payload"}</button>}
      >
        <div className="exec-prompt-payload-summary" data-qid="scillm-exec-graph:prompt-payload:summary">
          <strong>Payload summary</strong>
          <span>{hasPromptPayload ? promptPayloadFields : "No prompt-bearing fields for this node."}</span>
          <span>Node {node.id} · graph-local preview rendered {formatTimestamp(promptRenderedAt)}</span>
          <code>Local prompt payload hash - not execution output evidence: {promptPayloadHash}</code>
        </div>
        {node.review_scopes?.length ? (
          <div className="exec-prompt-scope-table" data-qid="scillm-exec-graph:prompt-payload:scope-summary">
            <div className="exec-prompt-scope-header">Review scope summary</div>
            {node.review_scopes.map((scope, index) => (
              (() => {
                const contract = reviewContractName(scope);
                const scopeWarningIds = nodeWarnings
                  .filter((issue) => issue.path?.includes(`review_scopes[${index}]`) || issue.contract === contract)
                  .map(warningIdentity);
                return (
                  <div className="exec-prompt-scope-row" key={`${scope.scope ?? "scope"}-${index}`}>
                    <span><b>Scope</b><strong>{scope.scope ?? scope.contract ?? `scope ${index + 1}`}</strong></span>
                    <span><b>Scope index</b>{`review_scopes[${index}]`}</span>
                    <span><b>Agent</b>{scope.agent ?? "missing agent"}</span>
                    <span><b>Model</b>{scope.model ?? "missing model"}</span>
                    <span><b>Proof floor</b>{scope.proof_level ?? "missing proof"}</span>
                    <span className={scope.prompt?.trim() ? "exec-scope-prompt-present" : "exec-scope-prompt-missing"}><b>Prompt status</b>{scope.prompt?.trim() ? "prompt present" : "prompt missing"}</span>
                    <span className={scopeWarningIds.length ? "exec-scope-warning" : "exec-scope-ok"}><b>Warning link</b>{scopeWarningIds.length ? `${scopeWarningIds.length} warning · ${scopeWarningIds[0]}` : "No scope warning"}</span>
                  </div>
                );
              })()
            ))}
          </div>
        ) : null}
        <div className="exec-prompt-payload-proof-strip" data-qid="scillm-exec-graph:prompt-payload:proof-strip">
          <div>
            <strong>Prompt payload provenance</strong>
            <code>Node {node.id} · Local prompt payload hash - not execution output evidence: {promptPayloadHash}</code>
          </div>
          <button className="exec-control-button exec-control-button-compact" data-qid="scillm-exec-graph:prompt-payload:copy-inline" data-qs-action="SCILLM_EXEC_PROMPT_PAYLOAD_COPY_INLINE" title={hasPromptPayload ? "Copy prompt payload JSON" : "No prompt payload to copy"} disabled={!hasPromptPayload} aria-disabled={!hasPromptPayload} onClick={() => void copyPromptPayload()}>{copied ? "Copied" : "Copy payload"}</button>
        </div>
        <details open={hasPromptPayload}>
          <summary style={{ cursor: "pointer", color: dimColor, fontSize: 12, marginBottom: 10 }}>Rendered JSON payload</summary>
          {hasPromptPayload ? <pre className="exec-json-pre" style={preStyle()}>{promptPayload}</pre> : <div className="exec-empty-state">No prompt payload for this node.</div>}
        </details>
      </Section>
    </div>
  );
}

function PlanDraftPanel({
  mode,
  validation,
  diff,
  runtimeReadiness,
  dirty,
  lastIssue,
  nicoProposals,
  draftGraph,
  auditLog,
  appliedProposalIds,
  canAmend,
  graphId,
  amendBackendLabel,
  amendState,
  warningsAcknowledged,
  formalDiffCopied,
  amendments,
  amendmentsState,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onReset,
  onExportDiff,
  onAmend,
  onRefreshAmendments,
  onLoadAmendment,
  onSetAmendmentStatus,
  onApplyAmendment,
  onWarningsAcknowledgedChange,
  onApplyProposal,
  onSelectNode,
}: {
  mode: DebuggerMode;
  validation: PlanValidationResult;
  diff: PlanDiffItem[];
  runtimeReadiness: RuntimeReadinessReport;
  dirty: boolean;
  lastIssue?: PlanValidationIssue;
  nicoProposals: NicoPlanProposal[];
  draftGraph: ExecGraph;
  auditLog: PlanAuditEntry[];
  appliedProposalIds: Set<string>;
  canAmend: boolean;
  graphId: string;
  amendBackendLabel: string;
  amendState: AmendState;
  warningsAcknowledged: boolean;
  formalDiffCopied: boolean;
  amendments: ExecGraphAmendment[];
  amendmentsState: AmendmentsLoadState;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  onReset: () => void;
  onExportDiff: () => void;
  onAmend: () => void;
  onRefreshAmendments?: () => void;
  onLoadAmendment: (amendment: ExecGraphAmendment) => void;
  onSetAmendmentStatus?: AmendmentStatusHandler;
  onApplyAmendment?: AmendmentApplyHandler;
  onWarningsAcknowledgedChange: (value: boolean) => void;
  onApplyProposal: (proposal: NicoPlanProposal) => void;
  onSelectNode: (nodeId: string) => void;
}) {
  useRegisterAction("scillm-exec-graph:plan-edit:reset", { app: "scillm", action: "SCILLM_EXEC_PLAN_RESET_DRAFT", label: "Reset draft" });
  useRegisterAction("scillm-exec-graph:plan-edit:undo", { app: "scillm", action: "SCILLM_EXEC_PLAN_UNDO", label: "Undo draft change" });
  useRegisterAction("scillm-exec-graph:plan-edit:redo", { app: "scillm", action: "SCILLM_EXEC_PLAN_REDO", label: "Redo draft change" });
  useRegisterAction("scillm-exec-graph:plan-edit:export-diff", { app: "scillm", action: "SCILLM_EXEC_PLAN_COPY_DIFF", label: "Copy plan diff" });
  useRegisterAction("scillm-exec-graph:plan-edit:apply-amendment", { app: "scillm", action: "SCILLM_EXEC_PLAN_APPLY_AMENDMENT", label: "Save amendment to Memory" });
  useRegisterAction("scillm-exec-graph:nico-proposal:apply", { app: "scillm", action: "SCILLM_EXEC_PLAN_APPLY_NICO_PROPOSAL", label: "Apply Nico proposal" });
  useRegisterAction("scillm-exec-graph:amendment:refresh", { app: "scillm", action: "SCILLM_EXEC_AMENDMENT_REFRESH", label: "Refresh amendments" });
  const warningCount = validation.warnings.length;
  const warningAckRequired = warningCount > 0;
  const diffHash = localIdentity(diff);
  const diffCountLabel = amendState.status === "saved" ? "Persisted diffs" : "Pending diffs";
  const amendDisabled = !dirty || !validation.canApply || !canAmend || amendState.status === "saving" || amendState.status === "saved" || (warningAckRequired && !warningsAcknowledged);
  const amendTitle = !canAmend
    ? "No shared Memory amendment backend is connected."
    : !dirty
      ? "No draft changes to save."
      : !validation.canApply
        ? "Blocking validation issues must be resolved first."
          : amendState.status === "saving"
            ? "Saving amendment record to shared Memory."
            : amendState.status === "saved"
              ? amendState.message
            : warningAckRequired && !warningsAcknowledged
              ? "Acknowledge the listed warnings before saving this amendment."
            : warningCount
              ? `Save this draft amendment with ${warningCount} accepted validation warning${warningCount === 1 ? "" : "s"}.`
              : "Save this draft amendment to shared Memory (ArangoDB) through /upsert.";
  const amendVisibleReason = !canAmend
    ? "Save unavailable: no shared Memory amendment backend is connected."
    : !dirty
      ? "Save unavailable: no draft changes."
      : !validation.canApply
        ? `Save blocked: ${validation.blocking[0]?.message ?? "blocking validation issue"}`
        : amendState.status === "saved"
          ? amendState.message
          : amendState.status === "error"
            ? `Memory amendment failed: ${amendState.message}`
            : warningAckRequired && !warningsAcknowledged
              ? `Save blocked: acknowledge ${warningCount} validation warning${warningCount === 1 ? "" : "s"} first.`
            : warningCount
              ? `Ready to save amendment · ${warningCount} warning${warningCount === 1 ? "" : "s"} requires acknowledgement`
              : "Ready to save amendment record; run evidence remains read-only.";
  const saveStatusLabel = !canAmend
    ? "No Memory backend"
    : amendState.status === "saved"
      ? "Memory status: Saved amendment record"
      : amendState.status === "error"
        ? "Memory status: Save failed"
        : amendState.status === "saving"
          ? "Memory status: Saving..."
          : "Memory status: Not saved";
  const saveStatusClass = amendState.status === "saved"
    ? "exec-plan-audit-status-ok"
    : !canAmend || amendState.status === "error"
      ? "exec-plan-audit-status-attention"
      : undefined;

  return (
    <div className="exec-plan-panel" data-qid={mode === "nico_proposals" ? "scillm-exec-graph:nico-proposals" : "scillm-exec-graph:plan-draft"}>
      <div className={dirty ? "exec-plan-audit-banner exec-plan-audit-banner-dirty" : "exec-plan-audit-banner"} data-qid="scillm-exec-graph:plan-audit-status">
        <strong>Draft revision</strong>
        <span className={dirty && amendState.status !== "saved" ? "exec-plan-audit-status-attention" : undefined}><b>Unsaved</b><em>{amendState.status === "saved" ? "No" : dirty ? "Yes" : "No"}</em></span>
        <span><b>Run evidence</b><em>Read-only</em></span>
        <span className={saveStatusClass} title="Saves the draft graph, structured diff, validation result, provenance, and audit metadata as a Memory amendment record."><b>Save</b><em>{saveStatusLabel}</em></span>
        <span className={diff.length ? "exec-plan-audit-status-attention exec-plan-audit-diff-status" : "exec-plan-audit-diff-status"}>
          <b>{diffCountLabel}</b>
          <em>{diff.length}</em>
          {diff.length ? (
            <button
              className="exec-plan-inline-action"
              type="button"
              onClick={() => document.querySelector('[data-qid="scillm-exec-graph:plan-diff"]')?.scrollIntoView({ block: "nearest" })}
              title="Jump to the draft diff evidence"
            >
              View formal diff
            </button>
          ) : null}
        </span>
        <span title="Memory /upsert stores one amendment record containing base graph hash, draft graph hash, diff, validation, actor, provenance, and tags."><b>Persistence</b>{amendBackendLabel}</span>
        <span><b>Audit source</b>Diff, validation, proposal provenance, local change log</span>
        <span><b>{amendState.status === "saved" ? "Saved diff hash" : "Draft diff hash"}</b><em>{diff.length ? diffHash : "none"}</em></span>
      </div>
      <div className="exec-plan-audit-log" data-qid="scillm-exec-graph:plan-audit-log">
        <div className="exec-plan-panel-heading">Local audit log</div>
        {auditLog.length ? (
          <div className="exec-plan-list">
            {auditLog.map((entry) => (
              <details key={entry.id} className="exec-plan-audit-entry">
                <summary>
                  <span>{formatEventTime(entry.ts)}</span>
                  <strong>{entry.action}</strong>
                  <span>{entry.actor}</span>
                  <span>{entry.diffRefs?.length ? `${entry.details} · produced ${entry.diffRefs.join(", ")}` : entry.details}</span>
                </summary>
                <pre className="exec-json-pre">{formatJsonBlock({ before: entry.before, after: entry.after })}</pre>
              </details>
            ))}
          </div>
        ) : <div className="exec-empty-state">No local draft changes recorded yet.</div>}
      </div>
      {amendState.status === "saved" ? (
        <div className="exec-plan-saved-memory" data-qid="scillm-exec-graph:plan-saved-memory">
          <strong>Saved Memory amendment</strong>
          <span className="exec-plan-saved-memory-primary"><b>Amendment key</b>{amendState.amendment_key ?? amendState.local_amendment_id}</span>
          <span className="exec-plan-saved-memory-primary"><b>Diff hash</b>{amendState.diff_hash}</span>
          <span className="exec-plan-saved-memory-primary"><b>Saved at UTC</b>{formatTimestamp(amendState.saved_at)}</span>
          <span><b>Graph</b>{amendState.graph_id}</span>
          <span><b>Status</b>proposed</span>
          <span><b>Actor</b>scillm-exec-graph-editor</span>
          <span><b>Diff count</b>{amendState.diff_count}</span>
          <span className={amendState.acknowledged_warning_ids.length ? "exec-plan-saved-memory-accepted-warning" : undefined}><b>Accepted warnings</b>{amendState.acknowledged_warning_ids.length ? amendState.acknowledged_warning_ids.join(", ") : "none"}</span>
          <span><b>Applied proposals</b>{amendState.proposal_ids.length ? amendState.proposal_ids.join(", ") : "none"}</span>
          <span><b>Mutation rule</b>Any new draft edit clears this saved identity and creates a new unsaved amendment draft.</span>
        </div>
      ) : null}
      <MemoryAmendmentsPanel
        amendments={amendments}
        state={amendmentsState}
        onRefresh={onRefreshAmendments}
        onLoadAmendment={onLoadAmendment}
        onSetAmendmentStatus={onSetAmendmentStatus}
        onApplyAmendment={onApplyAmendment}
      />
      {mode === "nico_proposals" ? (
        <div className="exec-plan-column">
          <div className="exec-plan-panel-heading">Nico proposals</div>
          {nicoProposals.length ? (
            <div className="exec-plan-list">
              {nicoProposals.map((proposal) => {
                const applied = appliedProposalIds.has(proposal.id);
                const proposalAudit = auditLog.find((entry) => entry.id.includes(proposal.id));
                const preview = applyNicoPlanProposal(draftGraph, proposal);
                const proposalDiff = preview.applied ? diffExecGraphPlan(draftGraph, preview.graph) : [];
                const affectedNodeIds = Array.from(new Set(proposal.patches.map((patch) => patch.op === "add_node" ? patch.node.id : patch.node_id)));
                return (
                  <div key={proposal.id} className={applied ? "exec-plan-proposal exec-plan-proposal-applied" : "exec-plan-proposal"} data-qid={`scillm-exec-graph:nico-proposal:${proposal.id}`}>
                    <div>
                      <strong>{proposal.title}</strong>
                      <div className="exec-plan-muted">Proposed by {proposal.proposed_by} · {proposal.patches.length} patch{proposal.patches.length === 1 ? "" : "es"}</div>
                      {proposal.rationale ? <div className="exec-plan-muted">{proposal.rationale}</div> : null}
                      {affectedNodeIds.length ? (
                        <div className="exec-plan-proposal-targets" aria-label="Proposal affected nodes">
                          <span>Affects</span>
                          {affectedNodeIds.map((nodeId) => (
                            <button key={nodeId} className="exec-link-button" type="button" onClick={() => onSelectNode(nodeId)} title={`Select affected node ${nodeId}`}>{nodeId}</button>
                          ))}
                        </div>
                      ) : null}
                      <div className="exec-plan-proposal-diff">
                        <span className="exec-info-label">Proposal-specific diff</span>
                        {applied ? proposal.patches.map((patch, index) => <span key={`${proposal.id}-patch-${index}`}>Applied patch: {patchSummary(patch)}</span>) : null}
                        {applied && proposalAudit ? <span>Applied at: {formatEventTime(proposalAudit.ts)} UTC · Produced: {proposalAudit.diffRefs?.join(", ") ?? "formal diff"} · Patch count: {proposal.patches.length}</span> : null}
                        {proposalDiff.length ? proposalDiff.map((item, index) => <span key={`${proposal.id}-${index}`}>Resulting formal diff: Diff {index + 1} · {item.kind.replaceAll("_", " ")}</span>) : <span>{preview.issue ? preview.issue.message : "This proposal's changes are incorporated into the draft."}</span>}
                      </div>
                    </div>
                    {applied ? <span className="exec-plan-status-badge">Applied</span> : <button className="exec-control-button exec-control-button-compact" type="button" data-qid={`scillm-exec-graph:nico-proposal:${proposal.id}:apply`} data-qs-action="SCILLM_EXEC_PLAN_APPLY_NICO_PROPOSAL" title={`Apply Nico proposal ${proposal.title} to draft`} onClick={() => onApplyProposal(proposal)}>Apply to draft</button>}
                  </div>
                );
              })}
            </div>
          ) : <div className="exec-empty-state">No Nico proposal source is connected for this graph.</div>}
        </div>
      ) : null}
      <div className="exec-plan-column" data-qid="scillm-exec-graph:plan-validation">
        <div className="exec-plan-panel-heading">Validation</div>
        <div className={validation.blocking.length ? "exec-plan-validation-summary exec-plan-validation-summary-blocking" : "exec-plan-validation-summary"}>
          Current validation: {validation.blocking.length} blocking · {validation.warnings.length} warning{validation.warnings.length === 1 ? "" : "s"}
        </div>
        {lastIssue ? <div className="exec-plan-issue exec-plan-rejected-patch"><strong>Rejected patch attempt — not applied to current draft</strong><span>{lastIssue.message}</span></div> : null}
        {validation.issues.length ? <ValidationIssueList issues={validation.issues} onSelectNode={onSelectNode} /> : <div className="exec-plan-issue exec-plan-issue-info">Draft is valid for amendment.</div>}
      </div>
      <div className="exec-plan-column" data-qid="scillm-exec-graph:runtime-readiness">
        <div className="exec-plan-panel-heading">Plan-iterate execution readiness</div>
        <div className={runtimeReadiness.can_execute_runtime ? "exec-plan-validation-summary" : "exec-plan-validation-summary exec-plan-validation-summary-blocking"}>
          Runtime readiness: {runtimeReadiness.summary.blocked_node_count} missing-field node{runtimeReadiness.summary.blocked_node_count === 1 ? "" : "s"} · {runtimeReadiness.summary.manual_node_count} manual node{runtimeReadiness.summary.manual_node_count === 1 ? "" : "s"}
        </div>
        {runtimeReadiness.nodes.filter((node) => node.missing_fields.length || node.status === "manual_action_required").length ? (
          <div className="exec-plan-list">
            {runtimeReadiness.nodes.filter((node) => node.missing_fields.length || node.status === "manual_action_required").map((node) => (
              <button key={node.node_id} className={`exec-plan-issue ${node.status === "runtime_ready" ? "exec-plan-issue-info" : "exec-plan-issue-blocking"}`} type="button" onClick={() => onSelectNode(node.node_id)} title={`Select ${node.node_id} to edit missing runtime fields`}>
                <span className="exec-plan-issue-code">{node.status.replaceAll("_", " ")}</span>
                <span className="exec-plan-issue-message">{node.node_id}: {node.missing_fields.length ? node.missing_fields.join(", ") : "manual action required"}</span>
              </button>
            ))}
          </div>
        ) : <div className="exec-plan-issue exec-plan-issue-info">All nodes have the fields needed for runtime compilation.</div>}
      </div>
      <div className="exec-plan-column" data-qid="scillm-exec-graph:plan-diff">
        <div className="exec-plan-panel-heading">Formal plan diff</div>
        {diff.length ? (
          <div className="exec-plan-list">
            {diff.map((item, index) => (
              <div key={`${item.kind}-${item.node_id}-${item.field ?? ""}-${index}`} className="exec-plan-diff-row">
                <strong>{item.kind.replaceAll("_", " ")}</strong>
                <span className="exec-plan-diff-index">Diff {index + 1} of {diff.length}</span>
                <span className="exec-plan-diff-detail">{item.label}</span>
                <span className="exec-plan-proposal-targets">
                  {diffNodeIds(item).map((nodeId) => (
                    <button key={nodeId} className="exec-link-button" type="button" onClick={() => onSelectNode(nodeId)} title={`Select diff node ${nodeId}`}>{nodeId}</button>
                  ))}
                </span>
                {(item.kind === "dependency_added" || item.kind === "dependency_removed") ? (
                  <div className="exec-plan-diff-before-after">
                    <span><b>Before dependencies</b><em>{dependencyList(item.before).join(", ") || "none"}</em></span>
                    <span><b>After dependencies</b><em>{dependencyList(item.after, item.kind === "dependency_added" ? item.dependency : undefined).join(", ") || "none"}</em></span>
                    {item.dependency ? <span className="exec-plan-obligation">Obligation: {item.node_id} must now consume evidence from {dependencyList(item.after).join(" and ")}.</span> : null}
                  </div>
                ) : null}
                {item.kind === "node_updated" ? (
                  <details className="exec-plan-json-diff">
                    <summary>Field JSON before/after</summary>
                    <pre className="exec-json-pre">{formatJsonBlock({ field: item.field, before: item.before, after: item.after })}</pre>
                  </details>
                ) : null}
              </div>
            ))}
          </div>
        ) : <div className="exec-empty-state">No draft changes.</div>}
      </div>
      <div className="exec-plan-actions">
        <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:undo" data-qs-action="SCILLM_EXEC_PLAN_UNDO" title={canUndo ? "Undo last draft change" : "No draft change to undo"} disabled={!canUndo} aria-disabled={!canUndo} onClick={onUndo}>Undo</button>
        <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:redo" data-qs-action="SCILLM_EXEC_PLAN_REDO" title={canRedo ? "Redo next draft change" : "No draft change to redo"} disabled={!canRedo} aria-disabled={!canRedo} onClick={onRedo}>Redo</button>
        <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:reset" data-qs-action="SCILLM_EXEC_PLAN_RESET_DRAFT" title={dirty ? "Reset draft to original evidence graph" : "No draft changes to reset"} disabled={!dirty} aria-disabled={!dirty} onClick={onReset}>Reset draft</button>
        <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:export-diff" data-qs-action="SCILLM_EXEC_PLAN_COPY_DIFF" title={dirty ? "Copy formal plan diff JSON with graph id, actor, timestamp, amendment identity, warning acknowledgements, proposal provenance, and diff hash." : "No draft changes to copy"} disabled={!dirty} aria-disabled={!dirty} onClick={onExportDiff}>{formalDiffCopied ? "Copied formal diff" : "Copy formal diff"}</button>
        <button className="exec-control-button exec-control-button-compact" type="button" data-qid="scillm-exec-graph:plan-edit:apply-amendment" data-qs-action="SCILLM_EXEC_PLAN_APPLY_AMENDMENT" disabled={amendDisabled} aria-disabled={amendDisabled} title={amendTitle} onClick={onAmend}>{amendState.status === "saving" ? "Memory status: Saving..." : amendState.status === "saved" ? "Saved amendment record" : warningCount ? `Save amendment with ${warningCount} warning${warningCount === 1 ? "" : "s"}` : "Save amendment record to Memory"}</button>
      </div>
      {warningAckRequired && amendState.status !== "saved" ? (
        <label className="exec-plan-warning-ack" data-qid="scillm-exec-graph:plan-warning-ack">
          <input type="checkbox" checked={warningsAcknowledged} onChange={(event) => onWarningsAcknowledgedChange(event.target.checked)} />
          <span>
            <strong>Warning acknowledgement required before save</strong>
            <span className="exec-plan-warning-intro">This amendment carries {warningCount} validation warning{warningCount === 1 ? "" : "s"}; each warning id is persisted with the amendment record.</span>
            <ul className="exec-plan-warning-list" data-qid="scillm-exec-graph:plan-warning-list">
              {validation.warnings.map((issue) => (
                <li key={warningIdentity(issue)}>
                  <code>{warningIdentity(issue)}</code>
                  <span>{issue.code === "missing_prompt_contract" ? `missing prompt contract for ${issue.node_id ?? "graph"}` : `${issue.code} for ${issue.node_id ?? "graph"}`}</span>
                </li>
              ))}
            </ul>
          </span>
        </label>
      ) : warningAckRequired && amendState.status === "saved" ? (
        <div className="exec-plan-warning-ack exec-plan-warning-ack-readonly" data-qid="scillm-exec-graph:plan-warning-ack">
          <strong>Accepted warning acknowledgement</strong>
          <span>Persisted acknowledgement: {amendState.acknowledged_warning_ids.join(", ")} accepted by scillm-exec-graph-editor at {formatTimestamp(amendState.saved_at)}.</span>
        </div>
      ) : null}
      <div className={validation.canApply && amendState.status !== "error" ? "exec-plan-amend-note" : "exec-plan-amend-note exec-plan-amend-note-blocking"}>{amendVisibleReason}</div>
    </div>
  );
}

function MemoryAmendmentsPanel({
  amendments,
  state,
  onRefresh,
  onLoadAmendment,
  onSetAmendmentStatus,
  onApplyAmendment,
}: {
  amendments: ExecGraphAmendment[];
  state: AmendmentsLoadState;
  onRefresh?: () => unknown | Promise<unknown>;
  onLoadAmendment: (amendment: ExecGraphAmendment) => void;
  onSetAmendmentStatus?: AmendmentStatusHandler;
  onApplyAmendment?: AmendmentApplyHandler;
}) {
  const [busyKey, setBusyKey] = useState<string | undefined>();
  const statusOptions: Array<Exclude<ExecGraphAmendmentStatus, "proposed">> = ["approved", "rejected", "superseded"];
  const loading = state.status === "loading";

  async function refresh() {
    if (!onRefresh || loading) return;
    setBusyKey("refresh");
    try {
      await onRefresh();
    } finally {
      setBusyKey(undefined);
    }
  }

  async function setStatus(amendment: ExecGraphAmendment, nextStatus: Exclude<ExecGraphAmendmentStatus, "proposed">) {
    if (!onSetAmendmentStatus || amendment.status === nextStatus) return;
    const nextBusyKey = `${amendment._key}:${nextStatus}`;
    setBusyKey(nextBusyKey);
    try {
      await onSetAmendmentStatus(amendment._key, nextStatus, `Marked ${nextStatus} from DAG editor.`);
    } finally {
      setBusyKey(undefined);
    }
  }

  async function applyAmendment(amendment: ExecGraphAmendment) {
    if (!onApplyAmendment || amendment.status !== "approved" || amendment.apply_status === "applied") return;
    const nextBusyKey = `${amendment._key}:apply`;
    setBusyKey(nextBusyKey);
    try {
      await onApplyAmendment(amendment, "Applied approved amendment from DAG editor.");
    } finally {
      setBusyKey(undefined);
    }
  }

  return (
    <div className="exec-plan-column" data-qid="scillm-exec-graph:memory-amendments">
      <div className="exec-plan-panel-heading exec-plan-panel-heading-row">
        <span>Memory amendments</span>
        <button
          className="exec-control-button exec-control-button-compact"
          type="button"
          data-qid="scillm-exec-graph:amendment:refresh"
          data-qs-action="SCILLM_EXEC_AMENDMENT_REFRESH"
          title={onRefresh ? "Refresh saved Memory amendments" : "No Memory amendment reader is connected."}
          disabled={!onRefresh || loading || busyKey === "refresh"}
          aria-disabled={!onRefresh || loading || busyKey === "refresh"}
          onClick={() => void refresh()}
        >
          {loading || busyKey === "refresh" ? "Refreshing" : "Refresh"}
        </button>
      </div>
      {state.status === "error" ? <div className="exec-plan-issue exec-plan-issue-warning">Memory amendment load failed: {state.message}</div> : null}
      {state.status !== "error" && state.message ? <div className="exec-plan-muted">{state.message}</div> : null}
      {amendments.length ? (
        <div className="exec-plan-list">
          {amendments.map((amendment) => {
            const canLoadDraft = Boolean(amendment.draft_graph);
            const timestamp = formatTimestamp(amendment.updated_at ?? amendment.created_at);
            const diffCount = amendment.diff?.length ?? 0;
            const applied = amendment.apply_status === "applied";
            const canApply = amendment.status === "approved" && !applied;
            const applyBusy = busyKey === `${amendment._key}:apply`;
            const applyDisabled = !onApplyAmendment || !canApply || applyBusy;
            const applyTitle = !onApplyAmendment
              ? "No Memory amendment apply writer is connected."
              : applied
                ? `Applied${amendment.applied_at ? ` at ${formatTimestamp(amendment.applied_at)}` : ""}.`
                : amendment.status !== "approved"
                  ? "Approve this amendment before applying it."
                  : "Apply this approved amendment as a provenance-recorded runtime decision overlay.";
            return (
              <div key={amendment._key} className="exec-plan-proposal exec-plan-amendment-record" data-qid={`scillm-exec-graph:amendment:${amendment._key}`}>
                <div>
                  <strong>{amendment._key}</strong>
                  <div className="exec-plan-muted">Graph {amendment.graph_id} · {timestamp}</div>
                  <div className="exec-plan-proposal-targets" aria-label="Amendment metadata">
                    <span>Status</span>
                    <b className={`exec-plan-status-badge exec-plan-status-badge-${amendment.status}`}>{amendment.status}</b>
                    <span>Diffs</span>
                    <b>{diffCount}</b>
                    <span>Apply</span>
                    <b className={`exec-plan-status-badge exec-plan-status-badge-${applied ? "approved" : "proposed"}`}>{applied ? "applied" : "not applied"}</b>
                    {amendment.actor ? <><span>Author</span><b>{amendment.actor}</b></> : null}
                  </div>
                  {amendment.status_reason ? <div className="exec-plan-muted">Status reason: {amendment.status_reason}</div> : null}
                  {amendment.status_actor ? <div className="exec-plan-muted">Status actor: {amendment.status_actor}</div> : null}
                  {applied ? <div className="exec-plan-muted">Applied by {amendment.applied_by ?? "unknown"} · {formatTimestamp(amendment.applied_at)} · graph {amendment.applied_graph_sha256?.slice(0, 16) ?? "hash unavailable"}</div> : null}
                  {amendment.apply_reason ? <div className="exec-plan-muted">Apply reason: {amendment.apply_reason}</div> : null}
                </div>
                <div className="exec-plan-amendment-actions">
                  <button
                    className="exec-control-button exec-control-button-compact"
                    type="button"
                    data-qid={`scillm-exec-graph:amendment:${amendment._key}:load`}
                    data-qs-action="SCILLM_EXEC_AMENDMENT_LOAD_DRAFT"
                    title={canLoadDraft ? "Load this saved amendment draft into the editor" : "This amendment record has no draft graph payload."}
                    disabled={!canLoadDraft}
                    aria-disabled={!canLoadDraft}
                    onClick={() => onLoadAmendment(amendment)}
                  >
                    Load draft
                  </button>
                  {statusOptions.map((nextStatus) => {
                    const buttonBusy = busyKey === `${amendment._key}:${nextStatus}`;
                    const disabled = !onSetAmendmentStatus || amendment.status === nextStatus || buttonBusy;
                    return (
                      <button
                        key={nextStatus}
                        className="exec-control-button exec-control-button-compact"
                        type="button"
                        data-qid={`scillm-exec-graph:amendment:${amendment._key}:${nextStatus}`}
                        data-qs-action="SCILLM_EXEC_AMENDMENT_SET_STATUS"
                        title={onSetAmendmentStatus ? `Mark amendment ${nextStatus}` : "No Memory amendment status writer is connected."}
                        disabled={disabled}
                        aria-disabled={disabled}
                        onClick={() => void setStatus(amendment, nextStatus)}
                      >
                        {buttonBusy ? "Saving" : nextStatus}
                      </button>
                    );
                  })}
                  <button
                    className="exec-control-button exec-control-button-compact"
                    type="button"
                    data-qid={`scillm-exec-graph:amendment:${amendment._key}:apply`}
                    data-qs-action="SCILLM_EXEC_AMENDMENT_APPLY"
                    title={applyTitle}
                    disabled={applyDisabled}
                    aria-disabled={applyDisabled}
                    onClick={() => void applyAmendment(amendment)}
                  >
                    {applyBusy ? "Applying" : applied ? "Applied" : "Apply"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : state.status === "loading" ? (
        <div className="exec-empty-state">Loading saved Memory amendments.</div>
      ) : (
        <div className="exec-empty-state">No saved Memory amendments for this graph.</div>
      )}
    </div>
  );
}

function ValidationIssueList({ issues, onSelectNode }: { issues: PlanValidationIssue[]; onSelectNode?: (nodeId: string) => void }) {
  return (
    <div className="exec-plan-list">
      {issues.map((issue, index) => (
        <div key={`${issue.code}-${issue.node_id ?? "graph"}-${index}`} className={`exec-plan-issue exec-plan-issue-${issue.severity}`}>
          <strong>{issue.severity.toUpperCase()} · {issue.code}</strong>
          <span className="exec-plan-issue-message">
            {issue.node_id && onSelectNode ? (
              <button className="exec-link-button" type="button" onClick={() => onSelectNode(issue.node_id!)} title={`Select node ${issue.node_id}`}>{issue.node_id}</button>
            ) : issue.node_id ? `${issue.node_id}: ` : ""}
            {issue.message}
          </span>
        </div>
      ))}
    </div>
  );
}

function Section({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return <section style={{ padding: 14, border: "1px solid var(--exec-border, rgba(255,255,255,0.14))", borderRadius: 8, background: "var(--exec-card, #1c2230)" }}><div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 12 }}><h4 style={{ margin: 0, fontSize: 14, fontWeight: 600, lineHeight: "20px" }}>{title}</h4>{action}</div><div style={{ display: "grid", gap: 12 }}>{children}</div></section>;
}

function Info({ label, value, children }: { label: string; value: string; children?: React.ReactNode }) {
  return <div className="exec-info-row"><div className="exec-info-label">{label}</div><div className="exec-info-value">{children ?? value}</div></div>;
}

function EvidenceInfo({ label, value, node, optional }: { label: string; value: unknown; node: ExecGraphNode; optional: boolean }) {
  const state = evidenceState(label, formatEvidenceValue(label, value), node, optional);
  return (
    <Info label={label} value={state.text}>
      <EvidenceBadge tone={state.tone} text={state.text} />
      {state.note ? <div className="exec-evidence-note">{state.note}</div> : null}
    </Info>
  );
}

function EvidenceBadge({ tone, text }: { tone: string; text: string }) {
  return <span className={`exec-evidence-badge exec-evidence-badge-${tone}`}>{text}</span>;
}

function preStyle(): React.CSSProperties {
  return { margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: "var(--exec-json-text, #d5deee)", fontSize: 11, lineHeight: 1.5 };
}

const execGraphDebuggerCss = `
.scillm-exec-debugger {
  --exec-dim-contrast: #b8c2d6;
  --exec-border-highlight: rgba(184, 194, 214, 0.72);
  --exec-disabled-fg: #e0e5ee;
  --exec-disabled-bg: #10151c;
  --exec-disabled-border: #3a4552;
  --exec-warning: #fff7ed;
  --exec-warning-strong: #fdba74;
  --exec-warning-border: #f59e0b;
  --exec-warning-bg: #4a2b06;
  --exec-warning-solid-text: #111827;
  --exec-warning-solid-bg: #fbbf24;
  --exec-warning-solid-border: #f59e0b;
  --exec-optional-border: #9ca35a;
  --exec-focus: #63b3ed;
  display: grid;
  grid-template-columns: minmax(520px, 1fr) 420px;
  min-height: 640px;
  background: var(--exec-bg, #0f1115);
  color: var(--exec-text, #e5e7eb);
  border: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  border-radius: 12px;
  overflow: hidden;
}
.scillm-exec-debugger * {
  box-sizing: border-box;
}
.scillm-exec-debugger :focus-visible {
  outline: 3px solid var(--exec-focus, #63b3ed);
  outline-offset: 3px;
  box-shadow: 0 0 0 6px rgba(99, 179, 237, 0.22);
}
.exec-control-button {
  min-height: 44px;
  min-width: 44px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  background: var(--exec-card, #1c2230);
  color: var(--exec-text, #e5e7eb);
  cursor: pointer;
  font: inherit;
  transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
}
.exec-control-button:hover {
  border-color: var(--exec-border-highlight);
  background: rgba(147,197,253,0.13);
}
.exec-control-button:disabled,
.exec-control-button[aria-disabled="true"] {
  color: var(--exec-disabled-fg, #8b96a8);
  background: repeating-linear-gradient(-45deg, #111721 0 8px, #151c28 8px 16px);
  border-color: var(--exec-disabled-border, #2d3748);
  cursor: not-allowed;
  transform: none;
  opacity: 1;
  box-shadow: inset 0 0 0 1px rgba(148,163,184,0.1);
}
.exec-control-button:disabled:hover,
.exec-control-button[aria-disabled="true"]:hover {
  border-color: var(--exec-disabled-border, #303642);
  background: var(--exec-disabled-bg, #1e2532);
}
.exec-control-button:active {
  background: rgba(255,255,255,0.1);
  transform: translateY(1px);
}
.exec-selected-neighborhood {
  position: absolute;
  z-index: 2;
  left: 14px;
  top: 52px;
  max-width: min(520px, calc(100% - 28px));
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  border: 1px solid rgba(34,211,238,0.38);
  background: rgba(12,18,28,0.92);
  color: var(--exec-text, #e5e7eb);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 16px;
}
.exec-selected-neighborhood strong {
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-neighborhood-group {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}
.exec-neighborhood-group b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-neighborhood-group em {
  color: var(--exec-dim-contrast);
  font-style: normal;
}
.exec-neighborhood-chip {
  min-height: 28px;
  border-radius: 999px;
  border: 1px solid rgba(34,211,238,0.45);
  background: rgba(34,211,238,0.1);
  color: #e5e7eb;
  cursor: pointer;
  padding: 3px 8px;
  font: inherit;
}
.exec-control-button-danger:hover {
  border-color: rgba(239, 68, 68, 0.75);
  background: rgba(239, 68, 68, 0.14);
}
.exec-control-button-compact {
  min-height: 44px;
  padding: 6px 10px;
  font-size: 12px;
}
.exec-controls-cluster {
  display: grid;
  justify-items: end;
  gap: 8px;
}
.exec-controls-reason {
  max-width: 300px;
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 16px;
  text-align: right;
}
.exec-controls-reason-error {
  color: var(--exec-failed, #ef4444);
}
.exec-node-button:hover {
  border-color: var(--exec-border-highlight) !important;
  background: color-mix(in srgb, var(--exec-card, #1c2230), #93c5fd 12%) !important;
}
.exec-node-button-selected {
  box-shadow: 0 0 0 4px var(--exec-card, #1c2230), 0 0 0 8px var(--exec-selected-ring, #22d3ee);
}
.exec-node-button-blocking {
  background: #23181b !important;
  box-shadow: 0 0 0 2px rgba(239,68,68,0.35), 0 0 18px rgba(239,68,68,0.28);
}
.exec-node-button:focus-visible {
  outline: 0;
  box-shadow: inset 0 0 0 3px var(--exec-focus, #63b3ed), 0 0 0 2px var(--exec-focus, #63b3ed), 0 0 0 7px rgba(99,179,237,0.22);
}
.exec-node-button-selected:focus-visible {
  box-shadow: inset 0 0 0 3px var(--exec-focus, #63b3ed), 0 0 0 4px var(--exec-card, #1c2230), 0 0 0 8px var(--exec-selected-ring, #22d3ee), 0 0 0 11px var(--exec-focus, #63b3ed);
}
.exec-node-button:active {
  transform: translateY(1px);
}
.exec-node-status {
  justify-self: start;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.16);
  padding: 2px 7px;
  font-size: 10px;
  line-height: 13px;
  color: var(--exec-text, #e5e7eb);
  background: rgba(255,255,255,0.06);
}
.exec-node-status-warning {
  color: var(--exec-warning-solid-text, #111827);
  border-color: var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  font-weight: 800;
}
.exec-node-badge-row {
  grid-column: 2;
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 3px 5px;
  min-width: 0;
  width: 100%;
}
.exec-node-optional-badge {
  justify-self: start;
  border-radius: 8px;
  border: 1px solid var(--exec-optional-border, #9ca35a);
  background: rgba(156,163,90,0.1);
  color: #d9df8f;
  padding: 2px 7px;
  font-size: 10px;
  line-height: 13px;
}
.exec-node-blocking-icon {
  grid-row: 1 / span 3;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: var(--exec-failed, #ef4444);
  color: #ffffff;
  font-size: 13px;
  font-weight: 800;
  line-height: 18px;
}
.exec-node-validation-badge {
  justify-self: start;
  border-radius: 8px;
  border: 1px solid var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  color: var(--exec-warning-solid-text, #111827);
  padding: 2px 7px;
  font-size: 10px;
  line-height: 13px;
  font-weight: 800;
}
.exec-node-validation-badge-blocking {
  border-color: var(--exec-failed, #ef4444);
  background: var(--exec-failed, #ef4444);
  color: #ffffff;
  font-weight: 800;
  text-transform: uppercase;
}
.exec-verdict-impact {
  min-height: 24px;
  display: inline-flex;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(250, 204, 21, 0.38);
  background: rgba(250, 204, 21, 0.1);
  color: var(--exec-warning, #facc15);
  padding: 2px 8px;
}
.exec-verdict-impact-failed {
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
  color: var(--exec-failed, #ef4444);
}
.exec-summary-chip {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.14);
  background: rgba(255,255,255,0.06);
  color: var(--exec-dim-contrast);
  padding: 4px 10px;
  font-size: 12px;
  font: inherit;
}
.exec-summary-label {
  color: var(--exec-dim-contrast);
  font-size: 12px;
  font-weight: 600;
}
button.exec-summary-chip {
  cursor: pointer;
}
.exec-summary-chip-action {
  margin-left: 8px;
  color: var(--exec-selected-ring, #22d3ee);
  border-color: rgba(34, 211, 238, 0.42);
  background: rgba(34, 211, 238, 0.08);
}
.exec-summary-chip-action:hover {
  background: rgba(34, 211, 238, 0.16);
}
.exec-summary-action-button {
  min-height: 44px;
  border: 1px solid rgba(34, 211, 238, 0.5);
  border-radius: 8px;
  background: rgba(34, 211, 238, 0.1);
  color: var(--exec-selected-ring, #22d3ee);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 6px 12px;
  margin-left: 8px;
}
.exec-summary-action-button:hover {
  border-color: var(--exec-selected-ring, #22d3ee);
  background: rgba(34, 211, 238, 0.18);
}
.exec-summary-chip-passed {
  color: var(--exec-passed, #22c55e);
  border-color: rgba(34,197,94,0.34);
}
.exec-summary-chip-warning {
  color: var(--exec-warning, #facc15);
  border-color: var(--exec-warning-border, #8a6a1f);
  background: var(--exec-warning-bg, #3a2a0a);
}
.exec-evidence-defect-queue {
  display: grid;
  gap: 8px;
  margin-top: 10px;
  border: 1px solid rgba(239,68,68,0.55);
  border-radius: 8px;
  background: rgba(127,29,29,0.18);
  padding: 10px;
}
.exec-evidence-defect-queue > strong {
  color: var(--exec-failed, #ef4444);
  font-size: 12px;
  line-height: 16px;
  text-transform: uppercase;
}
.exec-evidence-defect-queue > span {
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  line-height: 16px;
}
.exec-evidence-defect-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.exec-evidence-defect-item {
  min-height: 44px;
  max-width: 260px;
  display: inline-grid;
  gap: 2px;
  justify-items: start;
  border: 1px solid rgba(239,68,68,0.58);
  border-radius: 8px;
  background: rgba(239,68,68,0.12);
  color: var(--exec-text, #e5e7eb);
  padding: 6px 9px;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  line-height: 14px;
  text-align: left;
}
.exec-evidence-defect-item span {
  max-width: 100%;
  overflow-wrap: anywhere;
  font-weight: 800;
}
.exec-evidence-defect-item b {
  color: var(--exec-warning-strong, #fdba74);
  font-size: 10px;
}
.exec-evidence-defect-item-selected {
  border-color: var(--exec-selected-ring, #22d3ee);
  box-shadow: 0 0 0 2px rgba(34,211,238,0.2);
}
.exec-evidence-defect-item:hover {
  border-color: var(--exec-border-highlight);
  background: rgba(239,68,68,0.18);
}
.exec-inspector-header {
  border-left: 3px solid var(--exec-selected-ring, #22d3ee);
  padding-left: 10px;
}
.exec-inspector-sticky-summary {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 12px 12px 12px 10px;
  background: color-mix(in srgb, var(--exec-panel, #151923), #000 6%);
  border-bottom: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  box-shadow: 0 10px 18px rgba(0,0,0,0.24);
}
.exec-inspector-proof-summary {
  margin-top: 10px;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  line-height: 17px;
  font-weight: 600;
}
.exec-inspector-proof-summary-blocked {
  color: var(--exec-failed, #ef4444);
}
.exec-remediation-callout {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  border-radius: 8px;
  background: rgba(15,23,42,0.72);
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 1.45;
}
.exec-remediation-callout-blocking {
  border-color: rgba(239,68,68,0.62);
  background: rgba(127,29,29,0.22);
  color: var(--exec-text, #e5e7eb);
}
.exec-mode-badge-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 4px 0 8px;
}
.exec-compliance-summary-heading {
  margin-top: 12px;
  color: var(--exec-dim-contrast);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-compliance-issue-badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border-radius: 8px;
  border: 1px solid rgba(34,197,94,0.34);
  background: rgba(34,197,94,0.1);
  color: var(--exec-passed, #22c55e);
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 800;
}
.exec-compliance-issue-badge-blocking {
  border-color: var(--exec-failed, #ef4444);
  background: var(--exec-failed, #ef4444);
  color: #ffffff;
}
.exec-compliance-issue-badge-warning {
  border-color: var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  color: var(--exec-warning-solid-text, #111827);
}
.exec-readiness-field-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.exec-readiness-field {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  padding: 3px 7px;
  border: 1px solid var(--exec-failed, #ef4444);
  border-radius: 6px;
  background: rgba(127, 29, 29, 0.45);
  color: #fecaca;
  font-size: 11px;
  line-height: 15px;
  overflow-wrap: anywhere;
}
.exec-node-summary-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  margin-top: 12px;
}
.exec-info-row {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
}
.exec-info-label {
  color: var(--exec-dim-contrast);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.exec-info-value {
  min-width: 0;
  font-size: 13px;
  line-height: 18px;
  overflow-wrap: anywhere;
}
.exec-evidence-badge {
  display: inline-flex;
  min-height: 24px;
  align-items: center;
  border-radius: 999px;
  padding: 3px 8px;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.06);
  color: var(--exec-text, #e5e7eb);
}
.exec-evidence-badge-present {
  border-color: rgba(34,197,94,0.34);
  background: rgba(34,197,94,0.1);
  color: var(--exec-passed, #22c55e);
}
.exec-evidence-badge-optional {
  border-color: var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  color: var(--exec-warning-solid-text, #111827);
  font-weight: 800;
}
.exec-evidence-badge-missing {
  border-color: rgba(239,68,68,0.56);
  background: var(--exec-evidence-missing-bg, #3b1d1d);
  color: var(--exec-evidence-missing-text, #fecaca);
}
.exec-evidence-badge-na {
  border-color: rgba(209,213,219,0.18);
  background: var(--exec-evidence-na-bg, #1f2937);
  color: var(--exec-evidence-na-text, #d1d5db);
}
.exec-evidence-note {
  margin-top: 4px;
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 16px;
}
.exec-link-button {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(34, 211, 238, 0.42);
  border-radius: 999px;
  background: rgba(34, 211, 238, 0.08);
  color: var(--exec-selected-ring, #22d3ee);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 8px 12px;
}
.exec-link-button:hover {
  background: rgba(34, 211, 238, 0.16);
}
.exec-mode-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.exec-mode-tabs {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  border-radius: 8px;
  background: rgba(0,0,0,0.14);
}
.exec-mode-tab {
  min-height: 44px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--exec-dim-contrast);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 6px 10px;
}
.exec-mode-tab:hover {
  background: rgba(255,255,255,0.08);
  color: var(--exec-text, #e5e7eb);
}
.exec-mode-tab-active {
  background: rgba(34, 211, 238, 0.16);
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-plan-chip {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.14);
  padding: 4px 10px;
  font-size: 12px;
}
.exec-plan-chip-ok {
  color: var(--exec-passed, #22c55e);
  border-color: rgba(34,197,94,0.34);
  background: rgba(34,197,94,0.1);
}
.exec-plan-chip-blocking {
  color: var(--exec-failed, #ef4444);
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
}
.exec-plan-chip-readonly {
  color: var(--exec-dim-contrast);
  border-color: #3a4552;
  background: #222936;
}
.exec-plan-chip-dirty {
  color: var(--exec-warning, #facc15);
  border-color: var(--exec-warning-border, #8a6a1f);
  background: var(--exec-warning-bg, #3a2a0a);
}
.exec-header-draft-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.exec-header-save-precondition {
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  border: 1px solid rgba(250,204,21,0.65);
  background: rgba(250,204,21,0.1);
  color: #facc15;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 800;
}
.exec-header-save-precondition-ready {
  border-color: rgba(34,197,94,0.55);
  background: rgba(34,197,94,0.1);
  color: #22c55e;
}
.exec-plan-panel {
  display: grid;
  grid-template-columns: minmax(190px, 1fr) minmax(220px, 1.2fr) auto;
  gap: 16px;
  align-items: start;
  margin-bottom: 10px;
  padding: 10px;
  border: 1px solid var(--exec-border, rgba(255,255,255,0.14));
  border-radius: 8px;
  background: rgba(0,0,0,0.12);
}
.exec-plan-audit-banner {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 12px;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(34, 211, 238, 0.32);
  background: rgba(34, 211, 238, 0.08);
  color: var(--exec-text, #e5e7eb);
  padding: 12px 14px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-audit-banner strong {
  grid-column: 1 / -1;
  color: var(--exec-text, #e5e7eb);
  font-size: 13px;
}
.exec-plan-audit-banner span {
  display: grid;
  gap: 2px;
}
.exec-plan-audit-banner em {
  color: var(--exec-text, #e5e7eb);
  font-style: normal;
  font-weight: 650;
}
.exec-plan-audit-banner b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-plan-audit-banner-dirty {
  border-color: var(--exec-warning-border, #8a6a1f);
  background: var(--exec-warning-bg, #3a2a0a);
}
.exec-plan-audit-status-attention {
  border-left: 3px solid var(--exec-warning-border, #f59e0b);
  padding-left: 8px;
}
.exec-plan-audit-status-attention em {
  color: var(--exec-warning-strong, #fdba74);
}
.exec-plan-audit-status-ok em {
  color: #86efac;
}
.exec-plan-audit-diff-status {
  min-height: 44px;
}
.exec-plan-inline-action {
  justify-self: start;
  min-height: 28px;
  border: 1px solid rgba(253, 186, 116, 0.62);
  border-radius: 8px;
  background: rgba(253, 186, 116, 0.1);
  color: var(--exec-warning, #fff7ed);
  padding: 4px 8px;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}
.exec-plan-inline-action:hover {
  border-color: var(--exec-warning-strong, #fdba74);
  background: rgba(253, 186, 116, 0.18);
}
.exec-plan-audit-log {
  grid-column: 1 / -1;
  display: grid;
  gap: 8px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(0,0,0,0.14);
  padding: 8px;
}
.exec-plan-audit-entry {
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.04);
  color: var(--exec-text, #e5e7eb);
}
.exec-plan-audit-entry summary {
  min-height: 44px;
  display: grid;
  grid-template-columns: 72px minmax(100px, auto) minmax(90px, auto) minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  cursor: pointer;
  padding: 6px 8px;
  font-size: 13px;
  line-height: 18px;
}
.exec-plan-saved-memory {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px 14px;
  border-radius: 8px;
  border: 1px solid rgba(34,197,94,0.38);
  background: rgba(34,197,94,0.08);
  color: var(--exec-text, #e5e7eb);
  padding: 10px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-saved-memory strong {
  grid-column: 1 / -1;
  font-size: 13px;
}
.exec-plan-saved-memory span {
  display: grid;
  gap: 2px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.exec-plan-saved-memory-primary {
  border-radius: 8px;
  border: 1px solid rgba(34,197,94,0.42);
  background: rgba(34,197,94,0.12);
  padding: 8px;
}
.exec-plan-saved-memory-accepted-warning {
  border-radius: 8px;
  border: 1px solid rgba(34,211,238,0.38);
  background: rgba(34,211,238,0.1);
  padding: 8px;
}
.exec-plan-saved-memory b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-plan-column {
  min-width: 0;
  display: grid;
  gap: 16px;
}
.exec-plan-panel-heading {
  color: var(--exec-dim-contrast);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.exec-plan-panel-heading-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.exec-plan-list {
  display: grid;
  gap: 6px;
}
.exec-plan-field {
  display: grid;
  gap: 6px;
  color: var(--exec-dim-contrast);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.exec-plan-sensitive-group {
  display: grid;
  gap: 12px;
  border-radius: 8px;
  border: 2px solid #6a7d90;
  background: #1a2430;
  padding: 10px;
}
.exec-plan-subheading {
  color: #d5deee;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.exec-plan-obligation-summary {
  display: grid;
  gap: 8px;
  border-radius: 8px;
  border: 1px dashed rgba(34, 211, 238, 0.38);
  background: rgba(34, 211, 238, 0.08);
  padding: 8px;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  line-height: 16px;
  text-transform: none;
  letter-spacing: 0;
}
.exec-plan-obligation-summary span {
  display: grid;
  gap: 2px;
}
.exec-plan-obligation-summary b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.exec-review-scope-editor {
  display: grid;
  gap: 10px;
  border-radius: 8px;
  border: 1px solid rgba(34, 211, 238, 0.34);
  background: rgba(34, 211, 238, 0.06);
  padding: 10px;
}
.exec-review-scope-list {
  display: grid;
  gap: 10px;
}
.exec-review-scope-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: 8px;
  align-items: end;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(0,0,0,0.14);
  padding: 8px;
}
.exec-review-scope-toolbar {
  display: inline-flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.exec-review-scope-enabled {
  justify-items: start;
}
.exec-review-scope-enabled input {
  width: 44px;
  height: 44px;
  margin: 0;
  accent-color: var(--exec-selected-ring, #22d3ee);
}
.exec-review-scope-prompt {
  grid-column: 1 / -1;
}
.exec-review-scope-best-practices {
  grid-column: 1 / -1;
}
.exec-review-scope-best-practices-input {
  min-height: 64px;
}
.exec-review-scope-remove {
  align-self: end;
}
.exec-plan-inline-help,
.exec-plan-inline-warning {
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 16px;
  text-transform: none;
  letter-spacing: 0;
  font-weight: 500;
}
.exec-plan-inline-warning {
  justify-self: start;
  border-radius: 8px;
  border: 1px solid var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  color: var(--exec-warning-solid-text, #111827);
  padding: 4px 8px;
  font-weight: 800;
}
.exec-plan-input {
  width: 100%;
  min-height: 44px;
  border: 1px solid rgba(34, 211, 238, 0.48);
  border-radius: 8px;
  background: #202a3a;
  color: var(--exec-text, #e5e7eb);
  font: inherit;
  font-size: 13px;
  line-height: 18px;
  padding: 8px 10px;
  box-shadow: inset 0 0 0 1px rgba(34, 211, 238, 0.18);
}
.exec-plan-input:hover {
  border-color: rgba(34, 211, 238, 0.72);
  background: #263449;
}
.exec-plan-input:focus,
.exec-plan-input:focus-visible {
  outline: 3px solid var(--exec-focus, #63b3ed);
  outline-offset: 2px;
  box-shadow: 0 0 0 6px rgba(99, 179, 237, 0.22);
}
.exec-plan-textarea {
  min-height: 84px;
  resize: vertical;
}
.exec-plan-dependencies {
  display: grid;
  gap: 8px;
}
.exec-plan-dependency-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.exec-plan-add-dependency {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.exec-plan-remove-button {
  min-height: 44px;
  border: 1px solid rgba(239,68,68,0.5);
  border-radius: 999px;
  background: rgba(239,68,68,0.1);
  color: var(--exec-evidence-missing-text, #fecaca);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 3px 8px;
}
.exec-plan-available-dependencies {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.exec-plan-invalid-dependencies {
  display: grid;
  gap: 4px;
  border-radius: 8px;
  border: 1px solid rgba(148,163,184,0.34);
  background: rgba(148,163,184,0.08);
  color: var(--exec-dim-contrast);
  padding: 8px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-issue {
  display: grid;
  gap: 2px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  padding: 8px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-issue-blocking {
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
  color: var(--exec-evidence-missing-text, #fecaca);
}
.exec-plan-issue-warning {
  border-color: var(--exec-warning-solid-border, #f59e0b);
  background: var(--exec-warning-solid-bg, #fbbf24);
  color: var(--exec-warning-solid-text, #111827);
  box-shadow: inset 4px 0 0 #111827;
  font-weight: 800;
}
.exec-plan-issue-info {
  border-color: rgba(34,211,238,0.32);
  background: rgba(34,211,238,0.08);
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-plan-rejected-patch {
  border-color: rgba(148,163,184,0.34);
  background: rgba(148,163,184,0.08);
  color: var(--exec-text, #e5e7eb);
}
.exec-plan-validation-summary {
  min-height: 36px;
  display: flex;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(34,211,238,0.32);
  background: rgba(34,211,238,0.08);
  color: var(--exec-selected-ring, #22d3ee);
  padding: 6px 8px;
  font-size: 12px;
  line-height: 16px;
  font-weight: 800;
}
.exec-plan-validation-summary-blocking {
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
  color: var(--exec-evidence-missing-text, #fecaca);
}
.exec-plan-issue-message {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.exec-plan-diff-row,
.exec-plan-proposal {
  display: grid;
  gap: 3px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  padding: 8px;
  font-size: 12px;
  line-height: 16px;
  color: var(--exec-text, #e5e7eb);
}
.exec-plan-proposal {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}
.exec-plan-proposal-applied {
  border-color: rgba(34,197,94,0.34);
  background: rgba(34,197,94,0.08);
}
.exec-plan-amendment-record {
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
}
.exec-plan-amendment-record strong {
  overflow-wrap: anywhere;
}
.exec-plan-amendment-actions {
  display: flex;
  justify-content: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}
.exec-plan-proposal-diff {
  display: grid;
  gap: 8px;
  margin-top: 8px;
  border-radius: 8px;
  border: 1px dashed rgba(34, 211, 238, 0.32);
  padding: 8px;
  color: var(--exec-text, #e5e7eb);
}
.exec-plan-proposal-targets {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.exec-plan-proposal-targets span {
  color: var(--exec-dim-contrast);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-plan-proposal-diff .exec-info-label {
  border-bottom: 1px solid rgba(255,255,255,0.14);
  padding-bottom: 6px;
}
.exec-plan-status-badge {
  justify-self: end;
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  border: 1px solid rgba(34,197,94,0.42);
  background: rgba(34,197,94,0.12);
  color: var(--exec-passed, #22c55e);
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 800;
}
.exec-plan-status-badge-rejected {
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
  color: var(--exec-failed, #ef4444);
}
.exec-plan-status-badge-superseded {
  border-color: rgba(148,163,184,0.42);
  background: rgba(148,163,184,0.12);
  color: var(--exec-dim-contrast);
}
.exec-plan-status-badge-proposed {
  border-color: rgba(34,211,238,0.34);
  background: rgba(34,211,238,0.08);
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-plan-diff-detail {
  font-weight: 600;
}
.exec-plan-diff-before-after {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-top: 6px;
}
.exec-plan-diff-before-after span {
  display: grid;
  gap: 2px;
}
.exec-plan-diff-before-after b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.exec-plan-diff-before-after em {
  color: var(--exec-text, #e5e7eb);
  font-style: normal;
  overflow-wrap: anywhere;
}
.exec-plan-obligation {
  grid-column: 1 / -1;
  border-radius: 8px;
  background: rgba(34,211,238,0.08);
  border: 1px solid rgba(34,211,238,0.28);
  padding: 6px 8px;
  color: var(--exec-selected-ring, #22d3ee);
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-warning-ack {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  border-radius: 8px;
  border: 1px solid #d6a800;
  background: #fff8db;
  color: var(--exec-warning-solid-text, #111827);
  padding: 8px 10px;
  font-size: 12px;
  line-height: 16px;
  font-weight: 800;
}
.exec-plan-warning-ack input {
  width: 18px;
  height: 18px;
  margin-top: 1px;
}
.exec-plan-warning-ack strong {
  display: block;
  margin-bottom: 4px;
}
.exec-plan-warning-intro {
  display: block;
  margin-bottom: 6px;
}
.exec-plan-warning-list {
  display: grid;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
}
.exec-plan-warning-list li {
  padding-left: 2px;
}
.exec-plan-warning-list code {
  margin-right: 6px;
  font-size: 11px;
}
.exec-plan-warning-ack-readonly {
  grid-template-columns: minmax(0, 1fr);
  border-color: rgba(34,211,238,0.45);
  background: rgba(34,211,238,0.1);
  color: var(--exec-text, #e5e7eb);
}
.exec-plan-warning-ack-readonly strong {
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-plan-json-diff {
  margin-top: 6px;
}
.exec-plan-json-diff summary {
  min-height: 32px;
  cursor: pointer;
  color: var(--exec-selected-ring, #22d3ee);
  font-size: 12px;
}
.exec-prompt-payload-summary {
  display: grid;
  gap: 4px;
  border-radius: 8px;
  border: 1px solid rgba(34,211,238,0.35);
  background: rgba(34,211,238,0.08);
  padding: 8px 10px;
  margin-bottom: 10px;
  font-size: 12px;
}
.exec-prompt-payload-summary strong {
  color: var(--exec-selected-ring, #22d3ee);
}
.exec-prompt-payload-summary code {
  color: var(--exec-text, #e5e7eb);
  overflow-wrap: anywhere;
}
.exec-prompt-scope-table {
  display: grid;
  gap: 6px;
  margin-bottom: 10px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.04);
  padding: 8px;
  font-size: 12px;
}
.exec-prompt-scope-header {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-weight: 800;
}
.exec-prompt-scope-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
  align-items: center;
  min-width: 0;
  border-radius: 6px;
  background: rgba(0,0,0,0.14);
  padding: 5px 6px;
}
.exec-prompt-scope-row span {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
  min-width: 0;
  overflow-wrap: anywhere;
}
.exec-prompt-payload-proof-strip {
  position: sticky;
  top: 112px;
  z-index: 8;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(34,211,238,0.45);
  background: color-mix(in srgb, var(--exec-panel, #151923), #0e7490 18%);
  padding: 8px 10px;
  margin-bottom: 10px;
  box-shadow: 0 8px 18px rgba(0,0,0,0.28);
}
.exec-prompt-payload-proof-strip strong {
  display: block;
  color: var(--exec-selected-ring, #22d3ee);
  font-size: 11px;
  line-height: 15px;
  text-transform: uppercase;
}
.exec-prompt-payload-proof-strip code {
  display: block;
  color: var(--exec-text, #e5e7eb);
  font-size: 11px;
  line-height: 15px;
  overflow-wrap: anywhere;
}
.exec-prompt-scope-row b {
  color: var(--exec-dim-contrast);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.exec-prompt-scope-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
}
.exec-scope-prompt-present {
  color: var(--exec-passed, #22c55e);
}
.exec-scope-prompt-missing {
  color: var(--exec-warning, #facc15);
}
.exec-scope-warning {
  color: var(--exec-warning-strong, #fdba74);
}
.exec-scope-ok {
  color: var(--exec-passed, #22c55e);
}
.exec-draft-node-badge {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  border-radius: 8px;
  border: 1px solid rgba(56, 161, 105, 0.42);
  background: rgba(56, 161, 105, 0.18);
  color: #ffffff;
  padding: 3px 8px;
  font-size: 11px;
  line-height: 16px;
  font-weight: 800;
}
.exec-readonly-node-badge {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  border-radius: 8px;
  border: 1px solid #3a4552;
  background: #222936;
  color: var(--exec-dim-contrast);
  padding: 3px 8px;
  font-size: 11px;
  line-height: 16px;
  font-weight: 800;
}
.exec-source-node-badge {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.14);
  background: rgba(255,255,255,0.06);
  color: var(--exec-dim-contrast);
  padding: 3px 8px;
  font-size: 11px;
  line-height: 16px;
  font-weight: 700;
}
.exec-interview-question-list {
  display: grid;
  gap: 10px;
}
.exec-interview-question {
  display: grid;
  gap: 8px;
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 8px;
  background: rgba(255,255,255,0.04);
  padding: 10px;
}
.exec-interview-question-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  font-weight: 800;
}
.exec-interview-question-heading code {
  color: var(--exec-dim-contrast);
  font-size: 10px;
}
.exec-interview-question p {
  margin: 0;
  color: var(--exec-text, #e5e7eb);
  font-size: 13px;
  line-height: 18px;
}
.exec-interview-recommendation {
  color: var(--exec-selected-ring, #22d3ee);
  font-size: 12px;
  line-height: 18px;
}
.exec-interview-options {
  display: grid;
  gap: 6px;
  margin: 0;
}
.exec-interview-option {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 6px 8px;
  align-items: start;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  line-height: 18px;
  border-radius: 6px;
  padding: 5px 6px;
  background: rgba(0,0,0,0.14);
}
.exec-interview-option input {
  margin-top: 3px;
}
.exec-interview-options small {
  display: block;
  color: var(--exec-dim-contrast);
  grid-column: 2;
}
.exec-interview-commit-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  color: var(--exec-dim-contrast);
  font-size: 12px;
}
.exec-node-subagent-badge {
  border-radius: 999px;
  border: 1px solid rgba(34,211,238,0.34);
  background: rgba(34,211,238,0.12);
  color: var(--exec-selected-ring, #22d3ee);
  padding: 2px 6px;
  font-size: 9px;
  line-height: 12px;
  font-weight: 800;
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exec-subagent-event-list {
  display: grid;
  gap: 8px;
}
.exec-subagent-event {
  display: grid;
  gap: 5px;
  border: 1px solid rgba(34,211,238,0.2);
  border-radius: 8px;
  background: rgba(34,211,238,0.06);
  padding: 9px 10px;
}
.exec-subagent-event-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  font-weight: 800;
}
.exec-subagent-event-heading code {
  color: var(--exec-selected-ring, #22d3ee);
  font-size: 10px;
}
.exec-subagent-event p {
  margin: 0;
  color: var(--exec-text, #e5e7eb);
  font-size: 12px;
  line-height: 17px;
  overflow-wrap: anywhere;
}
.exec-subagent-event small {
  color: var(--exec-dim-contrast);
  font-size: 11px;
}
.exec-plan-muted {
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.exec-plan-commit-note {
  grid-column: 1 / -1;
  min-height: 32px;
  display: flex;
  align-items: center;
  border-radius: 8px;
  border: 1px solid var(--exec-disabled-border, #3a4552);
  background: var(--exec-disabled-bg, #10151c);
  color: var(--exec-disabled-fg, #e0e5ee);
  padding: 6px 8px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-amend-note {
  grid-column: 1 / -1;
  min-height: 32px;
  display: flex;
  align-items: center;
  border-radius: 8px;
  border: 1px solid rgba(34,211,238,0.32);
  background: rgba(34,211,238,0.08);
  color: var(--exec-selected-ring, #22d3ee);
  padding: 6px 8px;
  font-size: 12px;
  line-height: 16px;
}
.exec-plan-amend-note-blocking {
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
  color: var(--exec-evidence-missing-text, #fecaca);
}
.exec-summary-chip-failed {
  color: var(--exec-failed, #ef4444);
  border-color: rgba(239,68,68,0.5);
  background: rgba(239,68,68,0.12);
}
.exec-events-list {
  min-height: 92px;
  max-height: 174px;
  resize: vertical;
  overflow: auto;
  display: grid;
  gap: 4px;
  scrollbar-color: rgba(184,194,214,0.42) rgba(255,255,255,0.04);
}
.exec-event-row {
  display: grid;
  grid-template-columns: 10px 94px minmax(118px, auto) auto minmax(0, 1fr);
  gap: 14px;
  align-items: center;
  min-height: 52px;
  border: 0;
  background: transparent;
  color: var(--exec-text, #e5e7eb);
  font: inherit;
  font-size: 14px;
  line-height: 20px;
  text-align: left;
  cursor: pointer;
}
.exec-event-row:hover:not(:disabled) {
  color: var(--exec-text, #e5e7eb);
  background: rgba(255,255,255,0.05);
}
.exec-event-row-selected {
  color: var(--exec-text, #e5e7eb);
  background: rgba(34, 211, 238, 0.1);
  box-shadow: inset 3px 0 0 var(--exec-selected-ring, #22d3ee);
}
.exec-event-row-blocked {
  color: var(--exec-text, #e5e7eb);
  background: rgba(127,29,29,0.24);
  box-shadow: inset 3px 0 0 var(--exec-failed, #ef4444);
}
.exec-event-row:disabled {
  cursor: default;
  color: var(--exec-disabled-fg, #c0c8d6);
}
.exec-event-blocked-badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border-radius: 999px;
  border: 1px solid rgba(239,68,68,0.64);
  background: rgba(239,68,68,0.16);
  color: #fecaca;
  padding: 2px 7px;
  font-size: 10px;
  line-height: 14px;
}
.exec-event-row strong {
  color: var(--exec-text, #e5e7eb);
  font-weight: 600;
}
.exec-event-time {
  white-space: nowrap;
  color: var(--exec-dim-contrast);
  font-variant-numeric: tabular-nums;
}
.exec-event-filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--exec-dim-contrast);
}
.exec-event-filter-select {
  width: auto;
  min-width: 116px;
  min-height: 44px;
}
.exec-canvas-legend {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 1;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 999px;
  background: rgba(15,17,21,0.78);
  color: var(--exec-dim-contrast);
  padding: 3px 9px;
  font-size: 12px;
  line-height: 18px;
}
.exec-edge-label {
  fill: var(--exec-dim-contrast, #cbd5e1);
  stroke: rgba(15,17,21,0.88);
  stroke-width: 4px;
  paint-order: stroke;
  font-size: 11px;
  font-weight: 700;
  pointer-events: none;
}
.exec-edge-label-selected {
  fill: var(--exec-selected-ring, #22d3ee);
}
.exec-canvas-keyboard-hint {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 1;
  max-width: min(520px, calc(100% - 24px));
  border: 1px solid rgba(99,179,237,0.48);
  border-radius: 8px;
  background: rgba(15,17,21,0.86);
  color: var(--exec-text, #e5e7eb);
  padding: 6px 9px;
  font-size: 12px;
  line-height: 16px;
}
.exec-json-pre {
  max-height: 320px;
  overflow: auto;
  padding: 10px;
  border-radius: 8px;
  background: rgba(0,0,0,0.22);
  border: 1px solid rgba(255,255,255,0.08);
  scrollbar-color: rgba(184,194,214,0.42) rgba(255,255,255,0.04);
}
.exec-empty-state {
  min-height: 44px;
  display: flex;
  align-items: center;
  padding: 10px;
  border-radius: 8px;
  background: rgba(0,0,0,0.16);
  border: 1px dashed rgba(255,255,255,0.12);
  color: var(--exec-dim-contrast);
  font-size: 12px;
  line-height: 18px;
}
@media (max-width: 900px) {
  .scillm-exec-debugger {
    grid-template-columns: 1fr;
  }
  .exec-plan-panel {
    grid-template-columns: 1fr;
  }
  .exec-plan-audit-banner {
    grid-template-columns: 1fr;
  }
  .exec-plan-audit-entry summary {
    grid-template-columns: 1fr;
  }
}
`;

export default ScillmExecGraphDebugger;
