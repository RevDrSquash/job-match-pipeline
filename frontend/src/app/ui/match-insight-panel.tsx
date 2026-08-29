"use client";

import { useState } from "react";

import styles from "@/app/dashboard.module.css";
import JobDescriptionBody from "@/app/ui/job-description-body";
import type {
  AnalysisExperienceAsk,
  AnalysisLogisticsItem,
  AnalysisRequirement,
  JobDetail,
  Match,
  MatchAnalysis,
} from "@/lib/types";

const AXIS_LABELS: Record<string, string> = {
  location: "Location",
  arrangement: "Work arrangement",
  comp: "Compensation",
  authorization: "Work authorization",
  timezone: "Timezone",
};

function titleCase(value: string) {
  return value.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatAnalyzedAt(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

function formatYears(value: number | null | undefined) {
  if (value == null) return "not stated";
  return value === 1 ? "1 year" : `${value} years`;
}

function coverageClass(status: string) {
  if (status === "met") return styles.statusMet;
  if (status === "adjacent") return styles.statusAdjacent;
  if (status === "missing") return styles.statusMissing;
  return styles.statusUnclear;
}

function experienceClass(status: string) {
  if (status === "met") return styles.statusMet;
  if (status === "short") return styles.statusMissing;
  return styles.statusUnclear;
}

function logisticsClass(status: string) {
  if (status === "match") return styles.statusMet;
  if (status === "mismatch") return styles.statusMissing;
  return styles.statusUnclear;
}

function RequirementList({
  items,
  heading,
}: {
  items: AnalysisRequirement[];
  heading: string;
}) {
  const visible = items.filter((item) => item.requirement.trim());
  if (!visible.length) return null;
  return (
    <section className={styles.insightSection}>
      <h3>{heading}</h3>
      <ul className={styles.coverageList}>
        {visible.map((item, index) => (
          <li className={styles.coverageItem} key={`${item.requirement}-${index}`}>
            <span className={`${styles.statusMark} ${coverageClass(item.status)}`}>
              {titleCase(item.status || "unclear")}
            </span>
            <div>
              <p className={styles.coverageRequirement}>{item.requirement}</p>
              {item.evidence ? (
                <p className={styles.coverageEvidence}>{item.evidence}</p>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ExperienceItems({ items }: { items: AnalysisExperienceAsk[] }) {
  const visible = items.filter(
    (item) => item.skill.trim() || item.required_years != null || item.profile_years != null,
  );
  if (!visible.length) return null;
  return (
    <ul className={styles.coverageList}>
      {visible.map((item, index) => (
        <li className={styles.coverageItem} key={`${item.skill}-${index}`}>
          <span className={`${styles.statusMark} ${experienceClass(item.status)}`}>
            {titleCase(item.status || "unclear")}
          </span>
          <div>
            <p className={styles.coverageRequirement}>
              {item.skill || "Experience"}
              {item.kind === "preferred" ? " · preferred" : ""}
            </p>
            <p className={styles.coverageEvidence}>
              JD asks {formatYears(item.required_years)}; profile shows{" "}
              {formatYears(item.profile_years)}.
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function LogisticsList({ items }: { items: AnalysisLogisticsItem[] }) {
  if (!items.length) return null;
  return (
    <ul className={styles.coverageList}>
      {items.map((item, index) => (
        <li className={styles.coverageItem} key={`${item.axis}-${index}`}>
          <span className={`${styles.statusMark} ${logisticsClass(item.status)}`}>
            {titleCase(item.status || "unclear")}
          </span>
          <div>
            <p className={styles.coverageRequirement}>
              {AXIS_LABELS[item.axis] ?? titleCase(item.axis)}
            </p>
            <p className={styles.coverageEvidence}>
              JD: {item.jd || "not stated"} · Profile: {item.profile || "not stated"}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function BulletSection({
  heading,
  items,
  tone = "default",
}: {
  heading: string;
  items: string[];
  tone?: "default" | "warn" | "lead";
}) {
  const visible = items.map((item) => item.trim()).filter(Boolean);
  if (!visible.length) return null;
  const listClass =
    tone === "warn"
      ? `${styles.insightBullets} ${styles.insightWarn}`
      : tone === "lead"
        ? `${styles.insightBullets} ${styles.insightLead}`
        : styles.insightBullets;
  return (
    <section className={styles.insightSection}>
      <h3>{heading}</h3>
      <ul className={listClass}>
        {visible.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

type InsightPanelProps = {
  analysis: MatchAnalysis | null;
  analysisError: string;
  job: JobDetail | null;
  jobError: string;
  loadingAnalysis: boolean;
  loadingJob: boolean;
  match: Match | null;
  onRetryAnalysis: () => void;
  onRetryJob: () => void;
  panelId: string;
};

function MatchInsightContent({
  analysis,
  analysisError,
  job,
  jobError,
  loadingAnalysis,
  loadingJob,
  match,
  onRetryAnalysis,
  onRetryJob,
  panelId,
}: InsightPanelProps & { match: Match }) {
  const [jdOpen, setJdOpen] = useState(false);
  const report = analysis?.analysis;

  return (
    <>
      <header className={styles.jobViewerHeader}>
        <div>
          <p className={styles.eyebrow}>Match analysis</p>
          <h2>{match.title || "Untitled role"}</h2>
          <p className={styles.jobViewerMeta}>
            {match.company || "Company not listed"}
            {match.location ? ` · ${match.location}` : ""}
            {analysis?.created_at
              ? ` · Analyzed ${formatAnalyzedAt(analysis.created_at)}`
              : ""}
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
        {loadingAnalysis ? (
          <div className={styles.jobViewerState} role="status">
            <div className={styles.spinner} aria-hidden="true" />
            <p>Loading match analysis…</p>
          </div>
        ) : analysisError ? (
          <div className={styles.jobViewerState} role="alert">
            <span className={styles.emptyIcon}>!</span>
            <h3>We could not load this analysis</h3>
            <p>{analysisError}</p>
            <button className={styles.button} onClick={onRetryAnalysis} type="button">
              Try again
            </button>
          </div>
        ) : report ? (
          <div className={styles.insightReport}>
            <section className={styles.insightSection}>
              <h3>Fit judgment</h3>
              <p className={styles.insightVerdict}>{report.verdict}</p>
            </section>
            <RequirementList heading="Requirements" items={report.requirements ?? []} />
            <RequirementList heading="Nice to haves" items={report.nice_to_haves ?? []} />
            {(report.experience_alignment?.overall ||
              (report.experience_alignment?.items ?? []).length > 0) && (
              <section className={styles.insightSection}>
                <h3>Experience alignment</h3>
                {report.experience_alignment.overall ? (
                  <p className={styles.insightCopy}>
                    {report.experience_alignment.overall}
                  </p>
                ) : null}
                <ExperienceItems items={report.experience_alignment.items ?? []} />
              </section>
            )}
            {(report.logistics ?? []).length > 0 && (
              <section className={styles.insightSection}>
                <h3>Logistics</h3>
                <LogisticsList items={report.logistics} />
              </section>
            )}
            <BulletSection heading="Gaps to address" items={report.gaps_to_address ?? []} tone="warn" />
            <BulletSection heading="Lead with" items={report.emphasize ?? []} tone="lead" />
            <BulletSection heading="Red flags" items={report.red_flags ?? []} tone="warn" />
          </div>
        ) : (
          <div className={styles.insightPending}>
            <p className={styles.insightPendingLead}>
              Analysis queued — runs best-matches-first within the daily budget
            </p>
            {match.screen_reason ? (
              <div className={styles.gateReason}>
                <strong>Screen note</strong>
                {match.screen_reason}
              </div>
            ) : null}
          </div>
        )}

        <section className={styles.jdFold}>
          <button
            aria-controls={`${panelId}-jd`}
            aria-expanded={jdOpen}
            className={styles.jdToggle}
            onClick={() => setJdOpen((open) => !open)}
            type="button"
          >
            <span>Job description</span>
            <span>{jdOpen ? "Hide" : "Show"}</span>
          </button>
          {jdOpen && (
            <div className={styles.jdFoldBody} id={`${panelId}-jd`}>
              {loadingJob ? (
                <div className={styles.jobViewerState} role="status">
                  <div className={styles.spinner} aria-hidden="true" />
                  <p>Loading job description…</p>
                </div>
              ) : jobError ? (
                <div className={styles.jobViewerState} role="alert">
                  <span className={styles.emptyIcon}>!</span>
                  <h3>We could not load this description</h3>
                  <p>{jobError}</p>
                  <button className={styles.button} onClick={onRetryJob} type="button">
                    Try again
                  </button>
                </div>
              ) : job ? (
                <JobDescriptionBody html={job.raw_jd_html} text={job.raw_jd} />
              ) : null}
            </div>
          )}
        </section>
      </div>
    </>
  );
}

export default function MatchInsightPanel({
  analysis,
  analysisError,
  job,
  jobError,
  loadingAnalysis,
  loadingJob,
  match,
  onRetryAnalysis,
  onRetryJob,
  panelId,
}: InsightPanelProps) {
  const busy = loadingAnalysis || loadingJob;

  return (
    <aside aria-busy={busy} className={styles.jobViewer} id={panelId}>
      {match ? (
        <MatchInsightContent
          analysis={analysis}
          analysisError={analysisError}
          job={job}
          jobError={jobError}
          key={match.id}
          loadingAnalysis={loadingAnalysis}
          loadingJob={loadingJob}
          match={match}
          onRetryAnalysis={onRetryAnalysis}
          onRetryJob={onRetryJob}
          panelId={panelId}
        />
      ) : (
        <div className={styles.jobViewerState}>
          <span className={styles.emptyIcon}>↗</span>
          <h2>Select a match</h2>
          <p>Choose a role to read its qualification report and job description here.</p>
        </div>
      )}
    </aside>
  );
}
