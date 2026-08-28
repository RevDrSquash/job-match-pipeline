"use client";

import { useEffect, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import type { ForceGraphMethods } from "react-force-graph-2d";

import type { SkillGraphEdge, SkillGraphNode } from "@/lib/types";

type GraphNode = SkillGraphNode & { x?: number; y?: number };
type GraphLink = SkillGraphEdge;

const TYPE_COLORS: Record<string, string> = {
  skill: "--green",
  knowledge: "--blue",
  technology: "--amber",
  technology_category: "--muted",
  occupation: "--red",
};

/** Below this zoom (k), canvas labels are hidden; hover tooltips remain. */
const LABEL_MIN_SCALE = 0.75;
const CHARGE_STRENGTH = -560;
const LINK_DISTANCE = 150;

function readColor(variable: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(variable)
    .trim();
  return value || fallback;
}

function nodeColor(node: SkillGraphNode) {
  const variable = TYPE_COLORS[node.concept_type] ?? "--ink";
  return readColor(variable, "#17231e");
}

function truncateLabel(label: string, max = 34) {
  if (label.length <= max) return label;
  return `${label.slice(0, max - 1)}…`;
}

export default function SkillGraphCanvas({
  nodes,
  edges,
  selectedId,
  onSelect,
}: {
  nodes: SkillGraphNode[];
  edges: SkillGraphEdge[];
  selectedId: string;
  onSelect: (conceptId: string) => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(
    undefined,
  );
  const [size, setSize] = useState({ width: 640, height: 520 });
  const fitted = useRef(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect) return;
      setSize({
        width: Math.max(1, Math.floor(rect.width)),
        height: Math.max(1, Math.floor(rect.height)),
      });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    fitted.current = false;
  }, [nodes, edges]);

  useEffect(() => {
    const graph = graphRef.current;
    if (!graph) return;

    const charge = graph.d3Force("charge");
    if (charge && typeof charge.strength === "function") {
      charge.strength(CHARGE_STRENGTH);
    }
    if (charge && typeof charge.distanceMax === "function") {
      charge.distanceMax(900);
    }

    const link = graph.d3Force("link");
    if (link && typeof link.distance === "function") {
      link.distance(LINK_DISTANCE);
    }
    if (link && typeof link.strength === "function") {
      link.strength(0.35);
    }

    graph.d3ReheatSimulation();
  }, [edges, nodes, size.height, size.width]);

  return (
    <div ref={wrapRef} style={{ width: "100%", height: "100%" }}>
      <ForceGraph2D<GraphNode, GraphLink>
        ref={graphRef}
        width={size.width}
        height={size.height}
        backgroundColor={readColor("--paper", "#ffffff")}
        graphData={{ nodes: nodes as GraphNode[], links: edges }}
        nodeId="id"
        linkSource="source"
        linkTarget="target"
        nodeRelSize={6}
        warmupTicks={40}
        cooldownTicks={140}
        nodeLabel={(node) =>
          `${node.label} · ${node.concept_type.replaceAll("_", " ")}`
        }
        linkLabel={(link) => `${link.predicate} (${link.layer})`}
        linkColor={(link) =>
          link.layer === "canonical"
            ? readColor("--green", "#176b4d")
            : readColor("--muted", "#66736d")
        }
        linkLineDash={(link) => (link.layer === "source" ? [4, 3] : null)}
        linkWidth={(link) => (link.layer === "canonical" ? 1.6 : 1.1)}
        linkDirectionalArrowLength={4}
        linkDirectionalArrowRelPos={1}
        showPointerCursor={(item) =>
          Boolean(item && "layer" in item && item.layer === "canonical")
        }
        onNodeClick={(node) => {
          if (node.layer === "canonical") onSelect(node.id);
        }}
        onEngineStop={() => {
          if (fitted.current || nodes.length === 0) return;
          fitted.current = true;
          graphRef.current?.zoomToFit(400, 72);
        }}
        nodeCanvasObject={(node, ctx, globalScale) => {
          const x = node.x ?? 0;
          const y = node.y ?? 0;
          const radius =
            node.id === selectedId ? 8 : node.layer === "source" ? 6 : 5.5;
          ctx.beginPath();
          ctx.arc(x, y, radius, 0, Math.PI * 2);
          ctx.fillStyle = nodeColor(node);
          ctx.globalAlpha = node.layer === "source" ? 0.85 : 1;
          ctx.fill();
          ctx.globalAlpha = 1;
          if (node.id === selectedId) {
            ctx.lineWidth = 2;
            ctx.strokeStyle = readColor("--green-dark", "#0c4b35");
            ctx.stroke();
          }

          const showLabel =
            globalScale >= LABEL_MIN_SCALE || node.id === selectedId;
          if (!showLabel) return;

          const extra =
            typeof node.member_count === "number" && node.member_count > 0
              ? ` (${node.member_count})`
              : "";
          const label = truncateLabel(`${node.label}${extra}`);
          const fontSize = 11 / globalScale;
          ctx.font = `600 ${fontSize}px ${readColor("--font-geist-sans", "sans-serif")}, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = readColor("--ink", "#17231e");
          ctx.globalAlpha = node.id === selectedId ? 1 : 0.92;
          ctx.fillText(label, x, y + radius + 4 / globalScale);
          ctx.globalAlpha = 1;
        }}
        nodePointerAreaPaint={(node, color, ctx) => {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, 16, 0, Math.PI * 2);
          ctx.fill();
        }}
      />
    </div>
  );
}
