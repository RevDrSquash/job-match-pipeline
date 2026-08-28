"use client";

import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import styles from "@/app/skills.module.css";
import { fetchSkill, fetchSkillGraph, searchSkills } from "@/lib/api";
import { isCanonicalConceptId } from "@/lib/skills";
import type {
  SkillDetail,
  SkillGraphEdge,
  SkillGraphNode,
  SkillGraphPayload,
  SkillSearchHit,
  SkillStats,
} from "@/lib/types";

const SkillGraphCanvas = dynamic(() => import("@/app/ui/skill-graph-canvas"), {
  ssr: false,
  loading: () => (
    <div className={styles.canvasLoading}>Loading neighborhood graph…</div>
  ),
});

const ALIAS_ORDER = ["preferred", "alt", "curated", "derived"] as const;
const ALIAS_CLASS: Record<(typeof ALIAS_ORDER)[number], string> = {
  preferred: "",
  alt: styles.chipAlt,
  curated: styles.chipCurated,
  derived: styles.chipDerived,
};

const TYPE_SWATCH: Record<string, string> = {
  skill: "var(--green)",
  knowledge: "var(--blue)",
  technology: "var(--amber)",
  technology_category: "var(--muted)",
};

function formatCount(value: number | undefined) {
  return (value ?? 0).toLocaleString("en-US");
}

function conceptParam(value: string | null) {
  if (!value || !isCanonicalConceptId(value)) return "";
  return value;
}

export default function SkillGraphExplorer({
  stats,
  initialConceptId,
}: {
  stats: SkillStats | null;
  initialConceptId?: string;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedId =
    conceptParam(searchParams.get("concept")) ||
    conceptParam(initialConceptId ?? null);

  const searchRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillSearchHit[]>([]);
  const [resultQuery, setResultQuery] = useState("");
  const [searchError, setSearchError] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [graph, setGraph] = useState<SkillGraphPayload | null>(null);
  const [loadedKey, setLoadedKey] = useState("");
  const [selectionError, setSelectionError] = useState("");
  const [depth, setDepth] = useState<1 | 2>(1);
  const [showSource, setShowSource] = useState(true);

  const term = query.trim();
  const selectionKey = selectedId ? `${selectedId}:${depth}` : "";
  const searching = Boolean(term) && resultQuery !== term;
  const shownResults = resultQuery === term ? results : [];
  const shownSearchError = resultQuery === term ? searchError : "";
  const loadingSelection = Boolean(selectionKey) && loadedKey !== selectionKey;
  const shownError = loadedKey === selectionKey ? selectionError : "";
  const shownDetail =
    loadedKey === selectionKey && detail?.id === selectedId ? detail : null;
  const shownGraph = loadedKey === selectionKey ? graph : null;
  const showDropdown = searchOpen && Boolean(term);

  useEffect(() => {
    if (!term) return;
    let active = true;
    const timer = window.setTimeout(() => {
      searchSkills(term)
        .then((hits) => {
          if (!active) return;
          setResults(hits);
          setResultQuery(term);
          setSearchError("");
        })
        .catch((reason: unknown) => {
          if (!active) return;
          setResults([]);
          setResultQuery(term);
          setSearchError(
            reason instanceof Error ? reason.message : "Search failed.",
          );
        });
    }, 250);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [term]);

  useEffect(() => {
    if (!selectedId) return;
    const key = `${selectedId}:${depth}`;
    let active = true;
    Promise.all([fetchSkill(selectedId), fetchSkillGraph(selectedId, depth)])
      .then(([nextDetail, nextGraph]) => {
        if (!active) return;
        setDetail(nextDetail);
        setGraph(nextGraph);
        setLoadedKey(key);
        setSelectionError("");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setDetail(null);
        setGraph(null);
        setLoadedKey(key);
        setSelectionError(
          reason instanceof Error ? reason.message : "Unable to load this skill.",
        );
      });
    return () => {
      active = false;
    };
  }, [depth, selectedId]);

  useEffect(() => {
    if (!showDropdown) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!searchRef.current?.contains(event.target as Node)) {
        setSearchOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSearchOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [showDropdown]);

  const selectConcept = (conceptId: string) => {
    if (!isCanonicalConceptId(conceptId)) return;
    setQuery("");
    setSearchOpen(false);
    if (conceptId === selectedId) return;
    router.replace(`/skills?concept=${conceptId}`, { scroll: false });
  };

  const visible = useMemo(() => {
    if (!shownGraph) {
      return { nodes: [] as SkillGraphNode[], edges: [] as SkillGraphEdge[] };
    }
    if (showSource) return { nodes: shownGraph.nodes, edges: shownGraph.edges };
    const nodes = shownGraph.nodes.filter((node) => node.layer === "canonical");
    const kept = new Set(nodes.map((node) => node.id));
    const edges = shownGraph.edges.filter(
      (edge) =>
        edge.layer === "canonical" &&
        kept.has(edgeSourceId(edge.source)) &&
        kept.has(edgeSourceId(edge.target)),
    );
    return { nodes, edges };
  }, [showSource, shownGraph]);

  const conceptTotal = Object.values(stats?.concepts_by_type ?? {}).reduce(
    (sum, count) => sum + count,
    0,
  );
  const aliasTotal = Object.values(stats?.aliases_by_type ?? {}).reduce(
    (sum, count) => sum + count,
    0,
  );
  const canonicalEdges = stats?.edges.canonical ?? 0;

  return (
    <main className={styles.shell}>
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Skill taxonomy</p>
          <h1>Explore the canonical skill graph.</h1>
          <p>
            Search a skill, inspect its neighborhood, and see how ESCO and O*NET
            labels map onto the same concept. Whole-graph rendering is off —
            this view loads one neighborhood at a time.
          </p>
        </div>
        {stats && (
          <div className={styles.stats} aria-label="Taxonomy counts">
            <div className={styles.statChip}>
              <span>Concepts</span>
              <strong>{formatCount(conceptTotal)}</strong>
            </div>
            <div className={styles.statChip}>
              <span>Aliases</span>
              <strong>{formatCount(aliasTotal)}</strong>
            </div>
            <div
              className={`${styles.statChip} ${canonicalEdges === 0 ? styles.statWarn : ""}`}
            >
              <span>Canonical edges</span>
              <strong>{formatCount(canonicalEdges)}</strong>
            </div>
            <div className={styles.statChip}>
              <span>Source edges</span>
              <strong>{formatCount(stats.edges.source)}</strong>
            </div>
          </div>
        )}
      </div>

      <div className={styles.searchBar} ref={searchRef}>
        <label className={styles.searchLabel} htmlFor="skill-search">
          Search skills
        </label>
        <div className={styles.searchField}>
          <input
            aria-autocomplete="list"
            aria-controls="skill-search-results"
            aria-expanded={showDropdown}
            aria-label="Search skills"
            className={styles.searchBox}
            id="skill-search"
            onChange={(event) => {
              setQuery(event.target.value);
              setSearchOpen(true);
            }}
            onFocus={() => setSearchOpen(true)}
            placeholder="Docker, Python, AWS…"
            role="combobox"
            value={query}
          />
          {showDropdown && (
            <div
              className={styles.dropdown}
              id="skill-search-results"
              role="listbox"
            >
              {shownSearchError ? (
                <p className={styles.dropdownHint}>{shownSearchError}</p>
              ) : searching ? (
                <p className={styles.dropdownHint}>Searching…</p>
              ) : shownResults.length === 0 ? (
                <p className={styles.dropdownHint}>No matching concepts.</p>
              ) : (
                shownResults.map((hit) => (
                  <button
                    aria-selected={hit.id === selectedId}
                    className={`${styles.result} ${hit.id === selectedId ? styles.resultActive : ""}`}
                    key={hit.id}
                    onClick={() => selectConcept(hit.id)}
                    role="option"
                    type="button"
                  >
                    <strong>{hit.label}</strong>
                    <span className={styles.resultMeta}>
                      {hit.concept_type.replaceAll("_", " ")}
                      {hit.matched_alias !== hit.label
                        ? ` · matched “${hit.matched_alias}”`
                        : ""}
                    </span>
                  </button>
                ))
              )}
            </div>
          )}
        </div>
        <span className={styles.searchHint}>
          Exact aliases first, then similar names.
        </span>
      </div>

      <div className={styles.workspace}>
        <section
          className={`${styles.panel} ${styles.canvasPanel}`}
          aria-label="Neighborhood graph"
        >
          {shownError ? (
            <div className={styles.canvasEmpty}>
              <h2>Neighborhood unavailable</h2>
              <p>{shownError}</p>
            </div>
          ) : !selectedId ? (
            <div className={styles.canvasEmpty}>
              <h2>Search to start</h2>
              <p>
                Pick a concept to render its canonical neighbors and projected
                O*NET categories.
              </p>
            </div>
          ) : loadingSelection || !shownGraph ? (
            <div className={styles.canvasLoading}>Loading neighborhood graph…</div>
          ) : (
            <>
              {shownGraph.truncated && (
                <span className={styles.truncated}>Neighborhood truncated</span>
              )}
              <div className={styles.canvas}>
                <SkillGraphCanvas
                  edges={visible.edges}
                  nodes={visible.nodes}
                  onSelect={selectConcept}
                  selectedId={selectedId}
                />
              </div>
            </>
          )}
          <div className={styles.controls}>
            <div className={styles.control}>
              Depth
              <label>
                <input
                  checked={depth === 1}
                  onChange={() => setDepth(1)}
                  type="radio"
                  name="skill-depth"
                />
                1
              </label>
              <label>
                <input
                  checked={depth === 2}
                  onChange={() => setDepth(2)}
                  type="radio"
                  name="skill-depth"
                />
                2
              </label>
            </div>
            <label className={styles.control}>
              <input
                checked={showSource}
                onChange={(event) => setShowSource(event.target.checked)}
                type="checkbox"
              />
              Source-layer edges
            </label>
            <div className={styles.legend} aria-label="Legend">
              {Object.entries(TYPE_SWATCH).map(([type, color]) => (
                <span className={styles.legendItem} key={type}>
                  <span className={styles.swatch} style={{ background: color }} />
                  {type.replaceAll("_", " ")}
                </span>
              ))}
              <span className={styles.legendItem}>
                <span className={styles.swatchLine} />
                canonical IS_A
              </span>
              <span className={styles.legendItem}>
                <span className={styles.swatchDash} />
                source IS_A
              </span>
            </div>
          </div>
        </section>

        <section className={styles.panel} aria-label="Concept detail">
          <div className={styles.panelHeader}>
            <h2>Detail</h2>
            <span>Aliases and source provenance for the selected concept.</span>
          </div>
          {shownError ? (
            <div className={styles.errorState}>{shownError}</div>
          ) : !selectedId ? (
            <div className={styles.emptyDetail}>
              Select a skill to inspect aliases and ESCO / O*NET mappings.
            </div>
          ) : loadingSelection || !shownDetail ? (
            <div className={styles.emptyDetail}>Loading concept…</div>
          ) : (
            <div className={styles.detail}>
              <h3>{shownDetail.canonical_name}</h3>
              <div className={styles.typeRow}>
                <span className={styles.badge}>
                  {shownDetail.concept_type.replaceAll("_", " ")}
                </span>
                <span className={`${styles.badge} ${styles.badgeMuted}`}>
                  {shownDetail.status}
                </span>
              </div>
              {shownDetail.description ? (
                <p className={styles.description}>{shownDetail.description}</p>
              ) : (
                <p className={styles.hint}>No description stored for this concept.</p>
              )}

              {ALIAS_ORDER.map((aliasType) => {
                const aliases = shownDetail.aliases[aliasType] ?? [];
                if (!aliases.length) return null;
                return (
                  <div key={aliasType}>
                    <p className={styles.sectionLabel}>{aliasType} aliases</p>
                    <div className={styles.chips}>
                      {aliases.map((alias) => (
                        <span
                          className={`${styles.chip} ${ALIAS_CLASS[aliasType]}`}
                          key={`${aliasType}-${alias}`}
                        >
                          {alias}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })}

              <p className={styles.sectionLabel}>Source provenance</p>
              {shownDetail.sources.length === 0 ? (
                <p className={styles.hint}>No source mappings on this concept.</p>
              ) : (
                <div className={styles.tableWrap}>
                  <table className={styles.table}>
                    <thead>
                      <tr>
                        <th>Source</th>
                        <th>External id</th>
                        <th>Method</th>
                        <th>Conf.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shownDetail.sources.map((source) => (
                        <tr
                          key={`${source.source}:${source.source_version}:${source.external_id}`}
                        >
                          <td>
                            {source.source} {source.source_version}
                            <div className={styles.hint}>{source.name}</div>
                          </td>
                          <td className={styles.mono}>{source.external_id}</td>
                          <td>
                            {source.mapping_method}
                            <div className={styles.hint}>{source.mapping_type}</div>
                          </td>
                          <td>{Math.round(source.confidence * 100)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function edgeSourceId(endpoint: string | { id?: string }) {
  return typeof endpoint === "string" ? endpoint : (endpoint.id ?? "");
}
