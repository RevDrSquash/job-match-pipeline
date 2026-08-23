import styles from "@/app/dashboard.module.css";
import JobDescriptionBody from "@/app/ui/job-description-body";
import type { JobDetail, JobSummary } from "@/lib/types";

type ViewerSummary = Pick<JobSummary, "title" | "company" | "location">;

export default function JobDescriptionViewer({
  emptyDescription,
  emptyTitle,
  error,
  job,
  loading,
  onRetry,
  panelId,
  summary,
}: {
  emptyDescription: string;
  emptyTitle: string;
  error: string;
  job: JobDetail | null;
  loading: boolean;
  onRetry: () => void;
  panelId: string;
  summary: ViewerSummary | null;
}) {
  return (
    <aside aria-busy={loading} className={styles.jobViewer} id={panelId}>
      {summary ? (
        <>
          <header className={styles.jobViewerHeader}>
            <div>
              <p className={styles.eyebrow}>Job description</p>
              <h2>{summary.title || "Untitled role"}</h2>
              <p className={styles.jobViewerMeta}>
                {summary.company || "Company not listed"}
                {summary.location ? ` · ${summary.location}` : ""}
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
          <h2>{emptyTitle}</h2>
          <p>{emptyDescription}</p>
        </div>
      )}
    </aside>
  );
}
