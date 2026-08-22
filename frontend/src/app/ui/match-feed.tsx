"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchJob,
  fetchMatches,
  fetchUsers,
  recordMatchEvent,
  requestGeneration,
} from "@/lib/api";
import type { JobDetail, Match, SkillRef, User } from "@/lib/types";
import { skillDisplayLabel } from "@/lib/skills";
import styles from "@/app/dashboard.module.css";
import JobDescriptionBody from "@/app/ui/job-description-body";

const SKIP_REASONS = [
  ["not_interested", "Not interested"],
  ["wrong_location", "Wrong location"],
  ["wrong_comp", "Compensation"],
  ["wrong_seniority", "Seniority mismatch"],
  ["other", "Other"],
] as const;

const LABEL_COPY: Record<
  NonNullable<Match["qualification_label"]>,
  { title: string; tone: "high" | "mid" | "low" }
> = {
  clearly_qualified: { title: "Clearly qualified", tone: "high" },
  potentially_qualified: { title: "Potentially qualified", tone: "high" },
  overqualified: { title: "Overqualified", tone: "mid" },
  minimally_qualified: { title: "Minimally qualified", tone: "low" },
  unqualified: { title: "Unqualified", tone: "low" },
};

const LOW_LABELS = new Set<Match["qualification_label"]>([
  "unqualified",
  "minimally_qualified",
]);

function labelBadgeClass(label: Match["qualification_label"]) {
  if (!label) return styles.labelUnscreened;
  if (LABEL_COPY[label].tone === "high") return styles.labelHigh;
  if (LABEL_COPY[label].tone === "mid") return styles.labelMid;
  return styles.labelLow;
}

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
  const [selectedJobId, setSelectedJobId] = useState("");
  const [selectedJob, setSelectedJob] = useState<JobDetail | null>(null);
  const [loadingJobId, setLoadingJobId] = useState("");
  const [jobError, setJobError] = useState("");
  const viewedIds = useRef(new Set<string>());
  const jobCache = useRef(new Map<string, JobDetail>());
  const selectedJobIdRef = useRef("");
  const jobRequestId = useRef(0);

  const loadJob = useCallback((jobId: string, force = false) => {
    const requestId = ++jobRequestId.current;
    selectedJobIdRef.current = jobId;
    setSelectedJobId(jobId);

    if (!jobId) {
      setSelectedJob(null);
      setLoadingJobId("");
      setJobError("");
      return;
    }

    const cached = jobCache.current.get(jobId);
    if (cached && !force) {
      setSelectedJob(cached);
      setLoadingJobId("");
      setJobError("");
      return;
    }

    setSelectedJob(null);
    setLoadingJobId(jobId);
    setJobError("");
    fetchJob(jobId)
      .then((job) => {
        jobCache.current.set(jobId, job);
        if (jobRequestId.current === requestId) setSelectedJob(job);
      })
      .catch((reason: unknown) => {
        if (jobRequestId.current !== requestId) return;
        setJobError(
          reason instanceof Error ? reason.message : "Unable to load the job description.",
        );
      })
      .finally(() => {
        if (jobRequestId.current === requestId) setLoadingJobId("");
      });
  }, []);

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

  const matchKey = userId ? `${userId}:${retryCount}` : "";
  const loadingMatches = Boolean(userId && loadedMatchKey !== matchKey);

  useEffect(() => {
    if (!userId) return;
    let active = true;
    fetchMatches(userId)
      .then((rows) => {
        if (!active) return;
        setMatches(rows);
        const current = selectedJobIdRef.current;
        const nextJobId = rows.some((match) => match.job_id === current)
          ? current
          : (rows[0]?.job_id ?? "");
        loadJob(nextJobId);
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
  }, [loadJob, matchKey, userId]);

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

  const selectJob = (jobId: string) => {
    loadJob(jobId);
    if (window.matchMedia("(max-width: 850px)").matches) {
      requestAnimationFrame(() => {
        document
          .getElementById("match-job-description")
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
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
      if (result.action === "quota_exhausted") {
        setActionError("Resume quota is exhausted for this profile.");
        return;
      }
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
  const selectedMatch =
    matches.find((match) => match.job_id === selectedJobId) ?? null;

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
            <h2>Still scanning for matches</h2>
            <p>
              The local corpus starts small. Check back after the next matching
              cycle, or loosen your profile filters.
            </p>
            <Link className={styles.button} href="/profile">
              Review filters
            </Link>
          </div>
        </div>
      ) : (
        <div className={styles.matchWorkspace}>
          <div className={styles.matchColumn}>
            <div className={styles.matchList}>
              {matches.map((match) => (
                <article
                  className={`${styles.matchCard} ${
                    match.job_id === selectedJobId ? styles.selectedMatchCard : ""
                  }`}
                  key={match.id}
                  onClick={(event) => {
                    if (
                      (event.target as HTMLElement).closest(
                        "button, a, input, select, textarea",
                      )
                    ) {
                      return;
                    }
                    selectJob(match.job_id);
                  }}
                >
              <div>
                <div className={styles.matchTopline}>
                  <span
                    className={`${styles.score} ${
                      match.qualification_label &&
                      LOW_LABELS.has(match.qualification_label)
                        ? styles.rejectedScore
                        : ""
                    }`}
                  >
                    {match.rerank_score === null
                      ? "Unscored"
                      : `${Math.round(match.rerank_score * 100)}% match`}
                  </span>
                  <span
                    className={`${styles.labelBadge} ${labelBadgeClass(match.qualification_label)}`}
                  >
                    {match.qualification_label
                      ? LABEL_COPY[match.qualification_label].title
                      : "Unscreened"}
                  </span>
                  <span>{match.location || "Location not listed"}</span>
                  <span>·</span>
                  <span>{formatComp(match.comp_min, match.comp_max)}</span>
                </div>
                <button
                  aria-pressed={match.job_id === selectedJobId}
                  className={styles.matchHeadingButton}
                  onClick={() => selectJob(match.job_id)}
                  type="button"
                >
                  <span className={styles.matchTitle}>
                    {match.title || "Untitled role"}
                  </span>
                  <span className={styles.companyLine}>
                    {match.company || "Company not listed"}
                  </span>
                  <span className={styles.viewDescription}>
                    {match.job_id === selectedJobId
                      ? "Viewing description"
                      : "View description →"}
                  </span>
                </button>

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

                {match.screen_reason && (
                  <div className={styles.gateReason}>
                    <strong>Screen note</strong>
                    {match.screen_reason}
                  </div>
                )}
              </div>

              <div className={styles.cardActions}>
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
                {LOW_LABELS.has(match.qualification_label) &&
                  (match.ui.skipped?.reason_code === "disagree_with_gate" ? (
                    <p className={styles.statusNote}>Correction recorded</p>
                  ) : (
                    <button
                      className={styles.button}
                      disabled={busyMatch === match.id}
                      onClick={() => void disagreeWithGate(match.id)}
                    >
                      Actually, I qualify
                    </button>
                  ))}
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
                {match.ui.skipped &&
                match.ui.skipped.reason_code !== "disagree_with_gate" ? (
                  <p className={styles.statusNote}>Feedback saved</p>
                ) : !match.ui.skipped ? (
                  <button
                    className={`${styles.button} ${styles.quietButton}`}
                    onClick={() => setSkipMatch(match.id)}
                  >
                    Not for me
                  </button>
                ) : null}
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
          </div>

          <JobDescriptionViewer
            error={jobError}
            job={selectedJob?.id === selectedJobId ? selectedJob : null}
            loading={loadingJobId === selectedJobId}
            match={selectedMatch}
            onRetry={() => loadJob(selectedJobId, true)}
          />
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

function JobDescriptionViewer({
  error,
  job,
  loading,
  match,
  onRetry,
}: {
  error: string;
  job: JobDetail | null;
  loading: boolean;
  match: Match | null;
  onRetry: () => void;
}) {
  return (
    <aside
      aria-busy={loading}
      className={styles.jobViewer}
      id="match-job-description"
    >
      {match ? (
        <>
          <header className={styles.jobViewerHeader}>
            <div>
              <p className={styles.eyebrow}>Job description</p>
              <h2>{match.title || "Untitled role"}</h2>
              <p className={styles.jobViewerMeta}>
                {match.company || "Company not listed"}
                {match.location ? ` · ${match.location}` : ""}
              </p>
            </div>
            {job?.url && (
              <a
                className={styles.jobViewerLink}
                href={job.url}
                rel="noopener noreferrer"
                target="_blank"
              >
                Original posting ↗
              </a>
            )}
          </header>
          <div className={styles.jobViewerBody}>
            {loading ? (
              <div className={styles.jobViewerState} role="status">
                <div className={styles.spinner} aria-hidden="true" />
                <p>Loading job description…</p>
              </div>
            ) : error ? (
              <div className={styles.jobViewerState} role="alert">
                <span className={styles.emptyIcon}>!</span>
                <h3>We could not load this description</h3>
                <p>{error}</p>
                <button className={styles.button} onClick={onRetry} type="button">
                  Try again
                </button>
              </div>
            ) : job ? (
              <JobDescriptionBody html={job.raw_jd_html} text={job.raw_jd} />
            ) : null}
          </div>
        </>
      ) : (
        <div className={styles.jobViewerState}>
          <span className={styles.emptyIcon}>↗</span>
          <h2>Select a match</h2>
          <p>Choose a role to read its full job description here.</p>
        </div>
      )}
    </aside>
  );
}
