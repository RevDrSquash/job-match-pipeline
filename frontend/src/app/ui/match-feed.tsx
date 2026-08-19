"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import {
  fetchMatches,
  fetchUsers,
  recordMatchEvent,
  requestGeneration,
} from "@/lib/api";
import type { Match, SkillRef, User } from "@/lib/types";
import { skillDisplayLabel } from "@/lib/skills";
import styles from "@/app/dashboard.module.css";

const SKIP_REASONS = [
  ["not_interested", "Not interested"],
  ["wrong_location", "Wrong location"],
  ["wrong_comp", "Compensation"],
  ["wrong_seniority", "Seniority mismatch"],
  ["other", "Other"],
] as const;

function formatComp(min: number | null, max: number | null) {
  if (min === null && max === null) return "Comp not listed";
  const compact = (value: number) =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
      notation: "compact",
    }).format(value);
  if (min !== null && max !== null) return `${compact(min)}–${compact(max)}`;
  return min !== null ? `From ${compact(min)}` : `Up to ${compact(max!)}`;
}

function SkillChips({
  skills,
  tone = "matched",
}: {
  skills: SkillRef[];
  tone?: "matched" | "adjacent" | "missing";
}) {
  const toneClass =
    tone === "missing"
      ? styles.missingChip
      : tone === "adjacent"
        ? styles.adjacentChip
        : "";
  return (
    <div className={styles.chips}>
      {skills.length ? (
        skills.map((skill) => (
          <span className={`${styles.chip} ${toneClass}`} key={skill.id}>
            {skillDisplayLabel(skill)}
          </span>
        ))
      ) : (
        <span className={`${styles.chip} ${styles.emptyChip}`}>None listed</span>
      )}
    </div>
  );
}

export default function MatchFeed() {
  const router = useRouter();
  const [users, setUsers] = useState<User[]>([]);
  const [userId, setUserId] = useState("");
  const [view, setView] = useState<"matched" | "screened_out">("matched");
  const [matches, setMatches] = useState<Match[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [loadedMatchKey, setLoadedMatchKey] = useState("");
  const [retryCount, setRetryCount] = useState(0);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyMatch, setBusyMatch] = useState("");
  const [skipMatch, setSkipMatch] = useState("");
  const [skipReason, setSkipReason] = useState("not_interested");
  const [skipText, setSkipText] = useState("");
  const [queued, setQueued] = useState<string[]>([]);
  const viewedIds = useRef(new Set<string>());

  useEffect(() => {
    let active = true;
    fetchUsers()
      .then((rows) => {
        if (!active) return;
        setUsers(rows);
        if (rows.length === 1) setUserId(rows[0].id);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load users.");
      })
      .finally(() => {
        if (active) setLoadingUsers(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const matchKey = userId ? `${userId}:${view}:${retryCount}` : "";
  const loadingMatches = Boolean(userId && loadedMatchKey !== matchKey);

  useEffect(() => {
    if (!userId) return;
    let active = true;
    fetchMatches(userId, view)
      .then((rows) => {
        if (!active) return;
        setMatches(rows);
        setError("");
        setLoadedMatchKey(matchKey);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Unable to load matches.");
        setLoadedMatchKey(matchKey);
      });
    return () => {
      active = false;
    };
  }, [matchKey, userId, view]);

  useEffect(() => {
    if (!userId) return;
    const unseen = matches.filter(
      (match) => !match.ui.viewed && !viewedIds.current.has(match.id),
    );
    if (!unseen.length) return;

    unseen.forEach((match) => viewedIds.current.add(match.id));
    void Promise.all(
      unseen.map((match) =>
        recordMatchEvent(match.id, { user_id: userId, action: "viewed" }),
      ),
    ).catch(() => {
      // Exposure feedback should not interrupt the review workflow.
    });
  }, [matches, userId]);

  const updateUi = (matchId: string, patch: Partial<Match["ui"]>) => {
    setMatches((current) =>
      current.map((match) =>
        match.id === matchId
          ? { ...match, ui: { ...match.ui, ...patch } }
          : match,
      ),
    );
  };

  const submitEvent = async (
    matchId: string,
    body: Record<string, unknown>,
  ) => {
    setBusyMatch(matchId);
    setActionError("");
    try {
      await recordMatchEvent(matchId, { user_id: userId, ...body });
      return true;
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "That action could not be saved.");
      return false;
    } finally {
      setBusyMatch("");
    }
  };

  const submitSkip = async (matchId: string) => {
    const saved = await submitEvent(matchId, {
      action: "skipped",
      reason_code: skipReason,
      reason_text: skipText || undefined,
    });
    if (saved) {
      updateUi(matchId, {
        skipped: { reason_code: skipReason, reason_text: skipText || null },
      });
      setSkipMatch("");
      setSkipText("");
    }
  };

  const disagreeWithGate = async (matchId: string) => {
    const saved = await submitEvent(matchId, {
      action: "skipped",
      reason_code: "disagree_with_gate",
      reason_text: "User disagrees with the gate decision.",
    });
    if (saved) {
      updateUi(matchId, {
        skipped: {
          reason_code: "disagree_with_gate",
          reason_text: "User disagrees with the gate decision.",
        },
      });
    }
  };

  const generate = async (match: Match) => {
    if (match.generation_id) {
      router.push(`/generations/${match.generation_id}`);
      return;
    }

    setBusyMatch(match.id);
    setActionError("");
    try {
      await recordMatchEvent(match.id, {
        user_id: userId,
        action: "generate_requested",
      });
      const result = await requestGeneration(match.id);
      updateUi(match.id, { generate_requested: true });
      if (result.generation_id) {
        router.push(`/generations/${result.generation_id}`);
      } else {
        setQueued((current) =>
          current.includes(match.id) ? current : [...current, match.id],
        );
      }
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Generation could not be queued.");
    } finally {
      setBusyMatch("");
    }
  };

  const markApplied = async (match: Match) => {
    const saved = await submitEvent(match.id, { action: "marked_applied" });
    if (saved) updateUi(match.id, { applied_at: new Date().toISOString() });
  };

  const setOutcome = async (match: Match, outcome: "interview" | "rejected") => {
    const saved = await submitEvent(match.id, { action: "outcome", outcome });
    if (saved) updateUi(match.id, { outcome });
  };

  const noUserSelected = !loadingUsers && users.length > 1 && !userId;

  return (
    <main className={styles.shell}>
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Application workspace</p>
          <h1>Good roles, with the reasoning attached.</h1>
          <p>
            Review ranked matches, see where your experience lines up, and keep
            every application grounded in what you have actually done.
          </p>
        </div>
        {users.length > 1 && (
          <div className={styles.userSelect}>
            <label htmlFor="feed-user">Profile</label>
            <select
              id="feed-user"
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
            >
              <option value="">Choose a profile</option>
              {users.map((user, index) => (
                <option key={user.id} value={user.id}>
                  Profile {index + 1} · {user.tier}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div className={styles.toolbar}>
        <div className={styles.tabs} role="tablist" aria-label="Match views">
          <button
            className={`${styles.tab} ${view === "matched" ? styles.activeTab : ""}`}
            onClick={() => setView("matched")}
            role="tab"
            aria-selected={view === "matched"}
          >
            Recommended
          </button>
          <button
            className={`${styles.tab} ${view === "screened_out" ? styles.activeTab : ""}`}
            onClick={() => setView("screened_out")}
            role="tab"
            aria-selected={view === "screened_out"}
          >
            Screened out
          </button>
        </div>
        {!loadingMatches && userId && (
          <span className={styles.resultCount}>
            {matches.length} {matches.length === 1 ? "role" : "roles"}
          </span>
        )}
      </div>

      {loadingUsers || loadingMatches ? (
        <LoadingState />
      ) : error ? (
        <div className={styles.errorState}>
          <div>
            <span className={styles.emptyIcon}>!</span>
            <h2>We could not load this view</h2>
            <p>{error}</p>
            {userId && (
              <button
                className={styles.button}
                onClick={() => setRetryCount((current) => current + 1)}
              >
                Try again
              </button>
            )}
          </div>
        </div>
      ) : users.length === 0 ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>＋</span>
            <h2>Add your profile to begin</h2>
            <p>
              This local milestone uses the CLI for resume ingestion. Run{" "}
              <span className={styles.monospace}>jobmatch profile ingest</span>,
              then refresh this page.
            </p>
          </div>
        </div>
      ) : noUserSelected ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>↗</span>
            <h2>Choose a profile</h2>
            <p>Select the person whose matches you want to review.</p>
          </div>
        </div>
      ) : matches.length === 0 ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>⌁</span>
            <h2>
              {view === "matched"
                ? "Still scanning for matches"
                : "Nothing has been screened out"}
            </h2>
            <p>
              {view === "matched"
                ? "The local corpus starts small. Check back after the next matching cycle, or loosen your profile filters."
                : "Gate-rejected roles will appear here with the exact reason they were removed."}
            </p>
            {view === "matched" && (
              <Link className={styles.button} href="/profile">
                Review filters
              </Link>
            )}
          </div>
        </div>
      ) : (
        <div className={styles.matchList}>
          {matches.map((match) => (
            <article className={styles.matchCard} key={match.id}>
              <div>
                <div className={styles.matchTopline}>
                  <span
                    className={`${styles.score} ${
                      view === "screened_out" ? styles.rejectedScore : ""
                    }`}
                  >
                    {match.rerank_score === null
                      ? "Unscored"
                      : `${Math.round(match.rerank_score * 100)}% match`}
                  </span>
                  <span>{match.location || "Location not listed"}</span>
                  <span>·</span>
                  <span>{formatComp(match.comp_min, match.comp_max)}</span>
                </div>
                <h2 className={styles.matchTitle}>
                  {match.title || "Untitled role"}
                </h2>
                <p className={styles.companyLine}>
                  {match.company || "Company not listed"}
                </p>

                <div className={styles.skillRows}>
                  <div className={styles.skillRow}>
                    <strong>Matched</strong>
                    <SkillChips skills={match.matched_skills} />
                  </div>
                  {match.adjacent_skills.length > 0 && (
                    <div className={styles.skillRow}>
                      <strong>Adjacent</strong>
                      <SkillChips skills={match.adjacent_skills} tone="adjacent" />
                    </div>
                  )}
                  <div className={styles.skillRow}>
                    <strong>Missing</strong>
                    <SkillChips skills={match.missing_skills} tone="missing" />
                  </div>
                </div>

                {match.gate_reason && (
                  <div className={styles.gateReason}>
                    <strong>
                      {view === "screened_out" ? "Why it was screened" : "Gate note"}
                    </strong>
                    {match.gate_reason}
                  </div>
                )}
              </div>

              <div className={styles.cardActions}>
                {view === "screened_out" ? (
                  match.ui.skipped?.reason_code === "disagree_with_gate" ? (
                    <p className={styles.statusNote}>Correction recorded</p>
                  ) : (
                    <button
                      className={`${styles.button} ${styles.primaryButton}`}
                      disabled={busyMatch === match.id}
                      onClick={() => void disagreeWithGate(match.id)}
                    >
                      Actually, I qualify
                    </button>
                  )
                ) : (
                  <>
                    <button
                      className={`${styles.button} ${styles.primaryButton}`}
                      disabled={busyMatch === match.id || queued.includes(match.id)}
                      onClick={() => void generate(match)}
                    >
                      {match.generation_id
                        ? "Open application"
                        : queued.includes(match.id)
                          ? "Generation queued"
                          : "Generate resume"}
                    </button>
                    {match.ui.applied_at ? (
                      <>
                        <p className={styles.statusNote}>
                          Applied ·{" "}
                          {new Date(match.ui.applied_at).toLocaleDateString()}
                        </p>
                        <div className={styles.buttonRow}>
                          <button
                            className={`${styles.button} ${
                              match.ui.outcome === "interview"
                                ? styles.appliedButton
                                : ""
                            }`}
                            disabled={busyMatch === match.id}
                            onClick={() => void setOutcome(match, "interview")}
                          >
                            Interview
                          </button>
                          <button
                            className={`${styles.button} ${
                              match.ui.outcome === "rejected"
                                ? styles.dangerButton
                                : ""
                            }`}
                            disabled={busyMatch === match.id}
                            onClick={() => void setOutcome(match, "rejected")}
                          >
                            Rejected
                          </button>
                        </div>
                      </>
                    ) : (
                      <button
                        className={`${styles.button} ${styles.appliedButton}`}
                        disabled={busyMatch === match.id}
                        onClick={() => void markApplied(match)}
                      >
                        Mark as applied
                      </button>
                    )}
                    {match.ui.skipped ? (
                      <p className={styles.statusNote}>Feedback saved</p>
                    ) : (
                      <button
                        className={`${styles.button} ${styles.quietButton}`}
                        onClick={() => setSkipMatch(match.id)}
                      >
                        Not for me
                      </button>
                    )}
                  </>
                )}
              </div>

              {skipMatch === match.id && (
                <div className={styles.skipForm}>
                  <select
                    aria-label="Reason for skipping"
                    value={skipReason}
                    onChange={(event) => setSkipReason(event.target.value)}
                  >
                    {SKIP_REASONS.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                  <input
                    aria-label="Optional feedback"
                    placeholder="Optional note"
                    value={skipText}
                    onChange={(event) => setSkipText(event.target.value)}
                  />
                  <button
                    className={`${styles.button} ${styles.primaryButton}`}
                    disabled={busyMatch === match.id}
                    onClick={() => void submitSkip(match.id)}
                  >
                    Save
                  </button>
                  <button
                    className={styles.button}
                    onClick={() => setSkipMatch("")}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {actionError && (
        <div className={styles.toast} role="alert">
          {actionError}
        </div>
      )}
    </main>
  );
}

function LoadingState() {
  return (
    <div className={styles.loadingState}>
      <div>
        <div className={styles.spinner} aria-hidden="true" />
        <p>Loading your match workspace…</p>
      </div>
    </div>
  );
}
