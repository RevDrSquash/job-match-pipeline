"use client";

import { useEffect, useState } from "react";

import styles from "@/app/dashboard.module.css";
import { getAdminCompanies, getAdminJobs, runAdminJob } from "@/lib/api";
import type { AdminCompany, AdminJobId, AdminJobStatus } from "@/lib/types";

const POLL_MS = 3000;

const JOBS: Array<{
  id: AdminJobId;
  label: string;
  description: string;
  primary?: boolean;
}> = [
  {
    id: "fetch-link-list",
    label: "Fetch link lists",
    description: "Pull new postings from ATS boards.",
    primary: true,
  },
  {
    id: "match-incremental",
    label: "Match incremental",
    description: "Score jobs ingested since the last cycle.",
  },
  {
    id: "match-dirty",
    label: "Match dirty profiles",
    description: "Full corpus re-scan for edited profiles.",
  },
  {
    id: "analyze-batch",
    label: "Analyze batch",
    description: "Queue qualification reports for top matches.",
  },
];

function formatWhen(value: string | null) {
  if (!value) return null;
  return new Date(value).toLocaleString("en-US");
}

function formatResult(result: AdminJobStatus["last_result"]) {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (typeof result.error === "string") return result.error;
  if (typeof result.companies_total === "number") {
    const done = typeof result.companies_done === "number" ? result.companies_done : 0;
    const listed = typeof result.listed === "number" ? result.listed : 0;
    const enqueued = typeof result.enqueued === "number" ? result.enqueued : 0;
    return `${done}/${result.companies_total} companies · ${listed} listed · ${enqueued} enqueued`;
  }
  const parts = Object.entries(result)
    .filter(([, value]) =>
      typeof value === "number" || typeof value === "string" || typeof value === "boolean",
    )
    .slice(0, 4)
    .map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`);
  return parts.join(" · ");
}

function statusLine(job: AdminJobStatus | undefined) {
  if (!job) return "Status not loaded yet.";
  const result = formatResult(job.last_result);
  if (job.running) {
    return result ? `In progress — ${result}` : "In progress.";
  }
  const finished = formatWhen(job.finished_at);
  if (!finished && !result) return "Not run in this process.";
  if (finished && result) return `Last finished ${finished} — ${result}`;
  if (finished) return `Last finished ${finished}`;
  return result;
}

export default function AdminJobControls() {
  const [jobs, setJobs] = useState<AdminJobStatus[]>([]);
  const [companies, setCompanies] = useState<AdminCompany[]>([]);
  const [companyId, setCompanyId] = useState("");
  const [startingId, setStartingId] = useState<AdminJobId | "">("");
  const [alreadyRunning, setAlreadyRunning] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getAdminCompanies()
      .then((rows) => {
        if (!cancelled) setCompanies(rows);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Failed to load companies");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      getAdminJobs()
        .then((rows) => {
          if (cancelled) return;
          setJobs(rows);
          setError("");
        })
        .catch((cause: unknown) => {
          if (!cancelled) {
            setError(cause instanceof Error ? cause.message : "Failed to load jobs");
          }
        });
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const byId = new Map(jobs.map((job) => [job.id, job]));

  const onRun = async (jobId: AdminJobId) => {
    setAlreadyRunning("");
    setError("");
    setStartingId(jobId);
    try {
      const body =
        jobId === "fetch-link-list" && companyId
          ? { company_id: companyId }
          : undefined;
      const result = await runAdminJob(jobId, body);
      if (result.status === "already_running") {
        setAlreadyRunning(result.detail);
        return;
      }
      setJobs(await getAdminJobs());
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "Failed to start job");
    } finally {
      setStartingId("");
    }
  };

  return (
    <section className={`${styles.panel} ${styles.jobControls}`} aria-label="Pipeline jobs">
      <div className={styles.panelHeader}>
        <div>
          <h2>Pipeline jobs</h2>
          <span>Local stand-in for Cloud Scheduler. Status resets if the API restarts.</span>
        </div>
      </div>
      <div className={styles.panelBody}>
        <div className={styles.jobControlList}>
          {JOBS.map((spec) => {
            const job = byId.get(spec.id);
            const running = Boolean(job?.running);
            const disabled = running || startingId === spec.id;
            const resultLooksLikeError =
              typeof job?.last_result === "object" &&
              job.last_result !== null &&
              "error" in job.last_result;
            return (
              <div className={styles.jobControlRow} key={spec.id}>
                <div className={styles.jobControlMeta}>
                  <h3>{spec.label}</h3>
                  <p className={styles.jobControlStatus}>{spec.description}</p>
                  <p
                    className={
                      resultLooksLikeError && !running
                        ? `${styles.jobControlStatus} ${styles.jobControlError}`
                        : styles.jobControlStatus
                    }
                  >
                    {statusLine(job)}
                  </p>
                </div>
                <div className={styles.jobControlActions}>
                  {running ? (
                    <span className={styles.runningBadge} aria-live="polite">
                      <span className={styles.runningSpinner} aria-hidden="true" />
                      Running…
                    </span>
                  ) : null}
                  {spec.id === "fetch-link-list" ? (
                    <select
                      aria-label="Company to fetch"
                      disabled={disabled}
                      value={companyId}
                      onChange={(event) => setCompanyId(event.target.value)}
                    >
                      <option value="">All companies</option>
                      {companies.map((company) => (
                        <option key={company.id} value={company.id}>
                          {company.name}
                          {company.ats_provider ? ` (${company.ats_provider})` : ""}
                        </option>
                      ))}
                    </select>
                  ) : null}
                  <button
                    className={
                      spec.primary
                        ? `${styles.button} ${styles.primaryButton}`
                        : styles.button
                    }
                    disabled={disabled}
                    type="button"
                    onClick={() => void onRun(spec.id)}
                  >
                    {running ? "Running" : "Run"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
        {alreadyRunning ? (
          <p className={styles.alreadyRunningNote} role="status">
            Already running: {alreadyRunning}
          </p>
        ) : null}
        {error ? (
          <p className={`${styles.alreadyRunningNote} ${styles.jobControlError}`} role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </section>
  );
}
