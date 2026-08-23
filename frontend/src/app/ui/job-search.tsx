"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

import styles from "@/app/dashboard.module.css";
import JobDescriptionViewer from "@/app/ui/job-description-viewer";
import { fetchJob, searchJobs } from "@/lib/api";
import type { JobDetail, JobSummary } from "@/lib/types";

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

function formatPosted(value: string | null) {
  if (!value) return "Date not listed";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

export default function JobSearch() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [retryCount, setRetryCount] = useState(0);
  const [loadedKey, setLoadedKey] = useState("");
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [selectedJob, setSelectedJob] = useState<JobDetail | null>(null);
  const [loadingJobId, setLoadingJobId] = useState("");
  const [jobError, setJobError] = useState("");
  const jobCache = useRef(new Map<string, JobDetail>());
  const selectedJobIdRef = useRef("");
  const jobRequestId = useRef(0);
  const searchKey = `${query}:${retryCount}`;
  const loading = loadedKey !== searchKey;

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
    searchJobs(query)
      .then((rows) => {
        if (!active) return;
        setJobs(rows);
        const current = selectedJobIdRef.current;
        const nextJobId = rows.some((job) => job.id === current)
          ? current
          : (rows[0]?.id ?? "");
        loadJob(nextJobId);
        setError("");
        setLoadedKey(searchKey);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Unable to search jobs.");
        setJobs([]);
        setLoadedKey(searchKey);
      });
    return () => {
      active = false;
    };
  }, [loadJob, query, searchKey]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setQuery(draft.trim());
  };

  const selectJob = (jobId: string) => {
    loadJob(jobId);
    if (window.matchMedia("(max-width: 850px)").matches) {
      requestAnimationFrame(() => {
        document
          .getElementById("search-job-description")
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  };

  const selectedSummary = jobs.find((job) => job.id === selectedJobId) ?? null;

  return (
    <main className={styles.shell}>
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Local corpus</p>
          <h1>Search ingested jobs.</h1>
          <p>
            Keyword match across title, company, and location. Open a result to
            read the stored description and jump to the original posting.
          </p>
        </div>
      </div>

      <form className={styles.searchForm} onSubmit={onSubmit}>
        <label className={styles.srOnly} htmlFor="job-search">
          Search jobs
        </label>
        <input
          id="job-search"
          type="search"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Title, company, or location"
        />
        <button className={`${styles.button} ${styles.primaryButton}`} type="submit">
          Search
        </button>
      </form>

      <div className={styles.toolbar}>
        {!loading && (
          <span className={styles.resultCount}>
            {query
              ? `${jobs.length} ${jobs.length === 1 ? "result" : "results"} for “${query}”`
              : `${jobs.length} recent ${jobs.length === 1 ? "job" : "jobs"}`}
          </span>
        )}
      </div>

      {loading ? (
        <div className={styles.loadingState}>
          <div>
            <div className={styles.spinner} aria-hidden="true" />
            <p>Searching the local corpus…</p>
          </div>
        </div>
      ) : error ? (
        <div className={styles.errorState}>
          <div>
            <span className={styles.emptyIcon}>!</span>
            <h2>We could not search jobs</h2>
            <p>{error}</p>
            <button
              className={styles.button}
              onClick={() => setRetryCount((current) => current + 1)}
            >
              Try again
            </button>
          </div>
        </div>
      ) : jobs.length === 0 ? (
        <div className={styles.emptyState}>
          <div>
            <span className={styles.emptyIcon}>⌕</span>
            <h2>{query ? "No jobs matched that search" : "No jobs ingested yet"}</h2>
            <p>
              {query
                ? "Try a broader title, company, or location keyword."
                : "Ingest postings through the pipeline, then refresh this page."}
            </p>
          </div>
        </div>
      ) : (
        <div className={styles.matchWorkspace}>
          <div className={styles.matchColumn}>
            <div className={styles.matchList}>
              {jobs.map((job) => (
                <article
                  className={`${styles.matchCard} ${
                    job.id === selectedJobId ? styles.selectedMatchCard : ""
                  }`}
                  key={job.id}
                  onClick={(event) => {
                    if (
                      (event.target as HTMLElement).closest(
                        "button, a, input, select, textarea",
                      )
                    ) {
                      return;
                    }
                    selectJob(job.id);
                  }}
                >
                  <div>
                    <div className={styles.matchTopline}>
                      <span>{job.location || "Location not listed"}</span>
                      <span>·</span>
                      <span>{formatComp(job.comp_min, job.comp_max)}</span>
                      <span>·</span>
                      <span>{formatPosted(job.posted_at)}</span>
                    </div>
                    <button
                      aria-pressed={job.id === selectedJobId}
                      className={styles.matchHeadingButton}
                      onClick={() => selectJob(job.id)}
                      type="button"
                    >
                      <span className={styles.matchTitle}>
                        {job.title || "Untitled role"}
                      </span>
                      <span className={styles.companyLine}>
                        {job.company || "Company not listed"}
                      </span>
                      <span className={styles.viewDescription}>
                        {job.id === selectedJobId
                          ? "Viewing description"
                          : "View description →"}
                      </span>
                    </button>
                  </div>
                  <div className={styles.cardActions}>
                    <Link className={styles.button} href={`/jobs/${job.id}`}>
                      Open full page
                    </Link>
                  </div>
                </article>
              ))}
            </div>
          </div>

          <JobDescriptionViewer
            emptyDescription="Choose a job to read its full description here."
            emptyTitle="Select a job"
            error={jobError}
            job={selectedJob?.id === selectedJobId ? selectedJob : null}
            loading={loadingJobId === selectedJobId}
            onRetry={() => loadJob(selectedJobId, true)}
            panelId="search-job-description"
            summary={selectedSummary}
          />
        </div>
      )}
    </main>
  );
}
