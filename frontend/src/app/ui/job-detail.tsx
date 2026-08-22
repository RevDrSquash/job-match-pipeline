import Link from "next/link";

import styles from "@/app/dashboard.module.css";
import type { JobDetail as JobDetailPayload } from "@/lib/types";

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
  if (!value) return "Posted date not listed";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

export default function JobDetail({ job }: { job: JobDetailPayload }) {
  const extras = [
    job.seniority ? `Seniority: ${job.seniority}` : null,
    job.hard_requirements.length
      ? `Hard requirements: ${job.hard_requirements.join(", ")}`
      : null,
    job.nice_to_haves.length
      ? `Nice to haves: ${job.nice_to_haves.join(", ")}`
      : null,
  ].filter((item): item is string => item !== null);

  return (
    <main className={styles.narrowShell}>
      <Link className={styles.backLink} href="/jobs">
        ← Back to search
      </Link>
      <div className={styles.handoffHeader}>
        <div>
          <p className={styles.eyebrow}>Job description</p>
          <h1>{job.title || "Untitled role"}</h1>
          <p>
            {job.company || "Company not listed"}
            {job.location ? ` · ${job.location}` : ""}
          </p>
          <p>
            {formatPosted(job.posted_at)} · {formatComp(job.comp_min, job.comp_max)}
          </p>
        </div>
      </div>

      <div className={styles.handoffGrid}>
        <article className={styles.resumePaper}>
          {job.raw_jd_html ? (
            <div
              className={styles.jobDescriptionHtml}
              dangerouslySetInnerHTML={{ __html: job.raw_jd_html }}
            />
          ) : (
            <div className={styles.resumeDocument}>
              {job.raw_jd || "No job description was stored for this posting."}
            </div>
          )}
        </article>

        <aside className={styles.sideStack}>
          <section className={styles.sidePanel}>
            <h2>Original posting</h2>
            <p>
              The stored description is a local copy. Submit applications on the
              employer site.
            </p>
            {job.url ? (
              <a
                className={`${styles.button} ${styles.primaryButton}`}
                href={job.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open original posting ↗
              </a>
            ) : (
              <p>No source URL was stored for this job.</p>
            )}
          </section>

          {extras.length > 0 && (
            <section className={styles.sidePanel}>
              <h2>Extracted fields</h2>
              {extras.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </section>
          )}
        </aside>
      </div>
    </main>
  );
}
