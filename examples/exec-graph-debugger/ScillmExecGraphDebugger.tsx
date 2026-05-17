import React, { useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";

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
  messages?: Array<Record<string, unknown>>;
  output_schema?: Record<string, unknown>;
};

export type ExecGraph = {
  exec_graph_version: string;
  graph_id: string;
  graph_goal: string;
  nodes: ExecGraphNode[];
};

export type ExecStatus = {
  state?: string;
  node_results?: Record<string, Record<string, unknown>>;
};

export type ExecEvent = {
  ts?: string;
  type: string;
  node_id?: string;
  text?: string;
  state?: ExecNodeState;
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

function buildStates(graph: ExecGraph, status?: ExecStatus, events: ExecEvent[] = []): Record<string, ExecNodeState> {
  const states: Record<string, ExecNodeState> = {};
  for (const node of graph.nodes) states[node.id] = resultState(status?.node_results?.[node.id]);

  for (const event of events) {
    if (!event.node_id) continue;
    if (event.type === "node_scheduled") states[event.node_id] = "queued";
    if (event.type === "node_started") states[event.node_id] = "running";
    if (event.type === "breakpoint_hit") states[event.node_id] = "paused";
    if (event.type === "node_finished") states[event.node_id] = "passed";
    if (event.type === "node_failed") states[event.node_id] = "failed";
    if (event.type === "node_skipped") states[event.node_id] = "skipped";
    if (event.type === "needs_attention") states[event.node_id] = "needs_attention";
  }

  for (const node of graph.nodes) {
    if (states[node.id] !== "pending") continue;
    const deps = node.depends_on ?? [];
    if (deps.length === 0 || deps.every((dep) => states[dep] === "passed")) states[node.id] = "ready";
  }
  return states;
}

function layout(graph: ExecGraph, states: Record<string, ExecNodeState>, width: number, height: number) {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const depth = new Map<string, number>();
  const computeDepth = (id: string): number => {
    if (depth.has(id)) return depth.get(id)!;
    const node = byId.get(id);
    const deps = node?.depends_on ?? [];
    const value = deps.length === 0 ? 0 : Math.max(...deps.map(computeDepth)) + 1;
    depth.set(id, value);
    return value;
  };

  for (const node of graph.nodes) computeDepth(node.id);
  const maxDepth = Math.max(0, ...Array.from(depth.values()));
  const layers = d3.group(graph.nodes, (node) => depth.get(node.id) ?? 0);
  const xScale = d3.scalePoint<number>().domain(d3.range(maxDepth + 1)).range([120, Math.max(180, width - 120)]).padding(0.5);
  const nodes: LayoutNode[] = [];

  for (const [layer, layerNodes] of layers) {
    const yScale = d3.scalePoint<string>().domain(layerNodes.map((node) => node.id)).range([90, Math.max(140, height - 90)]).padding(0.8);
    for (const node of layerNodes) {
      nodes.push({ ...node, depth: layer, state: states[node.id] ?? "pending", x: xScale(layer) ?? 120, y: yScale(node.id) ?? height / 2 });
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
        path: line([[source.x + 78, source.y], [mid, source.y], [mid, target.y], [target.x - 78, target.y]]) ?? "",
      });
    }
  }
  return { nodes, edges };
}

export function ScillmExecGraphDebugger({ graph, status, events = [] }: { graph: ExecGraph; status?: ExecStatus; events?: ExecEvent[] }) {
  useRegisterAction("scillm-exec-graph:node:inspect", { app: "scillm", action: "SCILLM_EXEC_NODE_INSPECT", label: "Inspect node" });
  useRegisterAction("scillm-exec-graph:control:pause", { app: "scillm", action: "SCILLM_EXEC_GRAPH_PAUSE", label: "Pause graph" });
  useRegisterAction("scillm-exec-graph:control:resume", { app: "scillm", action: "SCILLM_EXEC_GRAPH_RESUME", label: "Resume graph" });
  useRegisterAction("scillm-exec-graph:control:stop", { app: "scillm", action: "SCILLM_EXEC_GRAPH_STOP", label: "Stop graph" });

  const { ref, size } = useSize();
  const [selectedId, setSelectedId] = useState(graph.nodes[0]?.id ?? "");
  const states = useMemo(() => buildStates(graph, status, events), [events, graph, status]);
  const { nodes, edges } = useMemo(() => layout(graph, states, size.width, size.height), [graph, size.height, size.width, states]);
  const selected = graph.nodes.find((node) => node.id === selectedId) ?? graph.nodes[0];

  return (
    <section data-qid="scillm-exec-graph:debugger" aria-label="scillm exec graph debugger" style={{ display: "grid", gridTemplateColumns: "minmax(520px, 1fr) 420px", minHeight: 640, background: "var(--exec-bg, #0f1115)", color: "var(--exec-text, #e5e7eb)", border: "1px solid var(--exec-border, rgba(255,255,255,0.14))", borderRadius: 14, overflow: "hidden" }}>
      <div style={{ display: "grid", gridTemplateRows: "auto 1fr auto", minWidth: 0 }}>
        <header style={{ padding: 16, background: "var(--exec-panel, #151923)", borderBottom: "1px solid var(--exec-border, rgba(255,255,255,0.14))" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <div>
              <div style={{ color: "var(--exec-dim, #94a3b8)", fontSize: 12, letterSpacing: 0.08, textTransform: "uppercase" }}>scillm exec graph</div>
              <h2 style={{ margin: "4px 0 0", fontSize: 18 }}>{graph.graph_id}</h2>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button data-qid="scillm-exec-graph:control:pause" data-qs-action="SCILLM_EXEC_GRAPH_PAUSE" title="Pause graph scheduling" style={buttonStyle()}>Pause</button>
              <button data-qid="scillm-exec-graph:control:resume" data-qs-action="SCILLM_EXEC_GRAPH_RESUME" title="Resume graph scheduling" style={buttonStyle()}>Resume</button>
              <button data-qid="scillm-exec-graph:control:stop" data-qs-action="SCILLM_EXEC_GRAPH_STOP" title="Stop graph run" style={buttonStyle()}>Stop</button>
            </div>
          </div>
          <p style={{ margin: "8px 0 0", color: "var(--exec-dim, #94a3b8)", fontSize: 13, lineHeight: 1.4 }}>{graph.graph_goal}</p>
        </header>

        <div ref={ref} style={{ minHeight: 0 }}>
          <svg role="img" aria-label="Live scillm exec DAG" viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="xMidYMid meet" style={{ display: "block", width: "100%", height: "100%" }}>
            <defs><marker id="exec-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--exec-dim, #94a3b8)" /></marker></defs>
            <g aria-hidden="true">{edges.map((edge) => <path key={edge.id} d={edge.path} fill="none" stroke="var(--exec-border, rgba(255,255,255,0.14))" strokeWidth={2} markerEnd="url(#exec-arrow)" />)}</g>
            <g>{nodes.map((node) => <GraphNode key={node.id} node={node} selected={node.id === selected?.id} onSelect={() => setSelectedId(node.id)} />)}</g>
          </svg>
        </div>

        <footer style={{ padding: 12, background: "var(--exec-panel, #151923)", borderTop: "1px solid var(--exec-border, rgba(255,255,255,0.14))" }}>
          <div style={{ color: "var(--exec-dim, #94a3b8)", fontSize: 12, marginBottom: 6 }}>Recent events</div>
          <div style={{ maxHeight: 104, overflow: "auto", display: "grid", gap: 4 }}>{events.slice(-8).reverse().map((event, index) => <code key={`${event.ts ?? "event"}-${index}`} style={{ color: "var(--exec-dim, #94a3b8)", fontSize: 11 }}>{event.ts ?? "no-ts"} · {event.type}{event.node_id ? ` · ${event.node_id}` : ""}</code>)}</div>
        </footer>
      </div>

      <aside data-qid="scillm-exec-graph:node-inspector" style={{ background: "var(--exec-panel, #151923)", borderLeft: "1px solid var(--exec-border, rgba(255,255,255,0.14))", overflow: "auto" }}>
        {selected ? <Inspector node={selected} state={states[selected.id] ?? "pending"} /> : <div style={{ padding: 16 }}>Select a node.</div>}
      </aside>
    </section>
  );
}

function GraphNode({ node, selected, onSelect }: { node: LayoutNode; selected: boolean; onSelect: () => void }) {
  return (
    <g role="button" tabIndex={0} data-qid={`scillm-exec-graph:node:${node.id}`} data-qs-action="SCILLM_EXEC_NODE_INSPECT" aria-label={`Inspect node ${node.id}, ${stateLabel[node.state]}`} title={`Inspect ${node.id}`} transform={`translate(${node.x}, ${node.y})`} onClick={onSelect} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelect(); }} style={{ cursor: "pointer" }}>
      <rect x={-82} y={-34} width={164} height={68} rx={14} fill="var(--exec-card, #1c2230)" stroke={selected ? "var(--exec-text, #e5e7eb)" : stateColor[node.state]} strokeWidth={selected ? 3 : 2} />
      <circle cx={-62} cy={-12} r={8} fill={stateColor[node.state]} />
      <text x={-46} y={-7} fill="var(--exec-text, #e5e7eb)" fontSize={12} fontWeight={700}>{node.id.length > 22 ? `${node.id.slice(0, 20)}…` : node.id}</text>
      <text x={-62} y={14} fill="var(--exec-dim, #94a3b8)" fontSize={10}>{node.type} · {stateLabel[node.state]}</text>
    </g>
  );
}

function Inspector({ node, state }: { node: ExecGraphNode; state: ExecNodeState }) {
  return (
    <div style={{ padding: 16, display: "grid", gap: 14 }}>
      <div>
        <div style={{ color: "var(--exec-dim, #94a3b8)", fontSize: 12, textTransform: "uppercase", letterSpacing: 0.08 }}>Node frame</div>
        <h3 style={{ margin: "4px 0", fontSize: 18 }}>{node.id}</h3>
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 12 }}><span aria-hidden style={{ width: 10, height: 10, borderRadius: 999, background: stateColor[state] }} />{stateLabel[state]}</span>
      </div>
      <Section title="Contract"><Info label="Goal" value={node.node_goal} /><Info label="Role" value={node.protocol_role ?? "worker"} /><Info label="Persona" value={node.persona_ref ?? "none"} /></Section>
      <Section title="Runtime"><Info label="Type" value={node.type} /><Info label="Model" value={node.model ?? "default"} /><Info label="Depends on" value={(node.depends_on ?? []).join(", ") || "none"} /></Section>
      <Section title="Prompt payload"><pre style={preStyle()}>{JSON.stringify({ prompt: node.prompt, messages: node.messages, output_schema: node.output_schema }, null, 2)}</pre></Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section style={{ padding: 12, border: "1px solid var(--exec-border, rgba(255,255,255,0.14))", borderRadius: 12, background: "var(--exec-card, #1c2230)" }}><h4 style={{ margin: "0 0 10px", fontSize: 13 }}>{title}</h4><div style={{ display: "grid", gap: 8 }}>{children}</div></section>;
}

function Info({ label, value }: { label: string; value: string }) {
  return <div><div style={{ color: "var(--exec-dim, #94a3b8)", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.06 }}>{label}</div><div style={{ fontSize: 13, lineHeight: 1.35 }}>{value}</div></div>;
}

function buttonStyle(): React.CSSProperties {
  return { minHeight: 36, padding: "8px 12px", borderRadius: 10, border: "1px solid var(--exec-border, rgba(255,255,255,0.14))", background: "var(--exec-card, #1c2230)", color: "var(--exec-text, #e5e7eb)", cursor: "pointer" };
}

function preStyle(): React.CSSProperties {
  return { margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: "var(--exec-dim, #94a3b8)", fontSize: 11, lineHeight: 1.45 };
}

export default ScillmExecGraphDebugger;
