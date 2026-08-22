"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import styles from "@/app/dashboard.module.css";
import { searchJobs } from "@/lib/api";
import type { JobSummary } from "@/lib/types";

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
  const searchKey = `${query}:${retryCount}`;
  const loading = loadedKey !== searchKey;

  useEffect(() => {
    let active = true;
    searchJobs(query)
      .then((rows) => {
        if (!active) return;
        setJobs(rows);
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
  }, [query, searchKey]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setQuery(draft.trim());
  };

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
        <div className={styles.matchList}>
          {jobs.map((job) => (
            <article className={styles.matchCard} key={job.id}>
              <div>
                <div className={styles.matchTopline}>
                  <span>{job.location || "Location not listed"}</span>
                  <span>·</span>
                  <span>{formatComp(job.comp_min, job.comp_max)}</span>
                  <span>·</span>
                  <span>{formatPosted(job.posted_at)}</span>
                </div>
                <h2 className={styles.matchTitle}>{job.title || "Untitled role"}</h2>
                <p className={styles.companyLine}>
                  {job.company || "Company not listed"}
                </p>
              </div>
              <div className={styles.cardActions}>
                <Link
                  className={`${styles.button} ${styles.primaryButton}`}
                  href={`/jobs/${job.id}`}
                >
                  View description
                </Link>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
