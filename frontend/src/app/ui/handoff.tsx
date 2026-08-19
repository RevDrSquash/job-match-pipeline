"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import styles from "@/app/dashboard.module.css";
import { recordMatchEvent } from "@/lib/api";
import type { Generation } from "@/lib/types";

function skillLabel(value: string) {
  const raw = value.split(":").at(-1) ?? value;
  return raw.replaceAll("_", " ").replaceAll("-", " ");
}

function verifyTone(status: string | null) {
  const normalized = status?.toLowerCase() ?? "pending";
  if (normalized === "passed" || normalized === "pass") return "";
  if (normalized === "failed" || normalized === "fail") return styles.verifyFailed;
  return styles.verifyPending;
}

function failureLabel(value: unknown) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    const code = typeof item.code === "string" ? item.code : "Verification issue";
    const detail =
      typeof item.message === "string"
        ? item.message
        : typeof item.detail === "string"
          ? item.detail
          : "";
    return detail ? `${code}: ${detail}` : code;
  }
  return String(value);
}

function buildContextBlock(generation: Generation) {
  const lines = [
    "# Application context",
    "",
    `Target role: ${generation.job.title ?? "Untitled role"}`,
    `Company: ${generation.job.company ?? "Company not listed"}`,
    `Location: ${generation.job.location ?? "Location not listed"}`,
    `Source: ${generation.job.url}`,
    "",
    "## Fit report",
    `Matched skills: ${generation.match.matched_skills.map(skillLabel).join(", ") || "None listed"}`,
    `Adjacent skills: ${generation.match.adjacent_skills.map(skillLabel).join(", ") || "None listed"}`,
    `Missing skills (do not claim): ${
      generation.match.missing_skills.map(skillLabel).join(", ") || "None listed"
    }`,
    `Gate reasoning: ${generation.match.gate_reason ?? "No gate note"}`,
    "",
    `Verification status: ${generation.verify_status ?? "pending"}`,
    "",
    "## Grounded resume",
    generation.resume_doc ?? "Resume generation is not complete.",
  ];
  return lines.join("\n");
}

export default function Handoff({ generation }: { generation: Generation }) {
  const [applied, setApplied] = useState(Boolean(generation.ui.applied_at));
  const [outcome, setOutcomeState] = useState<"interview" | "rejected" | null>(
    generation.ui.outcome,
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const contextBlock = useMemo(() => buildContextBlock(generation), [generation]);
  const status = generation.verify_status ?? "pending";
  const isFailed = ["failed", "fail"].includes(status.toLowerCase());

  const copyContext = async () => {
    try {
      await navigator.clipboard.writeText(contextBlock);
      setMessage("Context block copied.");
      setError("");
    } catch {
      setError("Clipboard access was unavailable. Select the context preview and copy it manually.");
    }
  };

  const downloadMarkdown = () => {
    const blob = new Blob([generation.resume_doc ?? ""], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${generation.job.company ?? "company"}-${
      generation.job.title ?? "resume"
    }.md`
      .toLowerCase()
      .replace(/[^a-z0-9.-]+/g, "-");
    link.click();
    URL.revokeObjectURL(url);
  };

  const markApplied = async () => {
    setBusy(true);
    setError("");
    try {
      await recordMatchEvent(generation.match_id, {
        user_id: generation.user_id,
        action: "marked_applied",
      });
      setApplied(true);
      setMessage("Application date recorded.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not record the application.");
    } finally {
      setBusy(false);
    }
  };

  const saveOutcome = async (value: "interview" | "rejected") => {
    setBusy(true);
    setError("");
    try {
      await recordMatchEvent(generation.match_id, {
        user_id: generation.user_id,
        action: "outcome",
        outcome: value,
      });
      setOutcomeState(value);
      setMessage(value === "interview" ? "Interview recorded." : "Outcome recorded.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not record the outcome.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className={styles.narrowShell}>
      <Link className={styles.backLink} href="/">
        ← Back to matches
      </Link>
      <div className={styles.handoffHeader}>
        <div>
          <p className={styles.eyebrow}>Application handoff</p>
          <h1>{generation.job.title ?? "Generated resume"}</h1>
          <p>
            {generation.job.company ?? "Company not listed"}
            {generation.job.location ? ` · ${generation.job.location}` : ""}
          </p>
        </div>
        <span className={`${styles.verifyBadge} ${verifyTone(status)}`}>
          Verification {status}
        </span>
      </div>

      {(isFailed || generation.verify_failures.length > 0) && (
        <section className={styles.warningPanel} role="alert">
          <h2>Verification needs your attention</h2>
          <p>
            Do not submit this resume until every flagged claim has been checked
            against your real experience.
          </p>
          {generation.verify_failures.length > 0 && (
            <ul>
              {generation.verify_failures.map((failure, index) => (
                <li key={`${failureLabel(failure)}-${index}`}>
                  {failureLabel(failure)}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <div className={styles.handoffGrid}>
        <article className={styles.resumePaper}>
          <div className={styles.resumeDocument}>
            {generation.resume_doc || "The resume is still being generated."}
          </div>
        </article>

        <aside className={styles.sideStack}>
          <section className={styles.sidePanel}>
            <h2>Application tools</h2>
            <p>
              Review the resume, then take it to the employer. Submission always
              stays in your hands.
            </p>
            <a
              className={`${styles.button} ${styles.primaryButton}`}
              href={generation.job.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open original posting ↗
            </a>
            <button
              className={styles.button}
              disabled={!generation.resume_doc}
              onClick={downloadMarkdown}
            >
              Download Markdown
            </button>
          </section>

          <section className={styles.sidePanel}>
            <h2>Paste-ready context</h2>
            <p>
              Includes the fit report, explicit missing-skill guardrails, and
              verified resume for your own browser tooling.
            </p>
            <div className={styles.contextPreview}>{contextBlock}</div>
            <button className={styles.button} onClick={() => void copyContext()}>
              Copy context block
            </button>
          </section>

          <section className={styles.sidePanel}>
            <h2>Application outcome</h2>
            {!applied ? (
              <>
                <p>Record this only after you submit on the employer site.</p>
                <button
                  className={`${styles.button} ${styles.appliedButton}`}
                  disabled={busy}
                  onClick={() => void markApplied()}
                >
                  Mark as applied
                </button>
              </>
            ) : (
              <>
                <p>
                  Applied today. No response is the default until you record an
                  outcome.
                </p>
                <div className={styles.outcomeBox}>
                  <button
                    className={`${styles.button} ${
                      outcome === "interview" ? styles.appliedButton : ""
                    }`}
                    disabled={busy}
                    onClick={() => void saveOutcome("interview")}
                  >
                    Got interview
                  </button>
                  <button
                    className={`${styles.button} ${
                      outcome === "rejected" ? styles.dangerButton : ""
                    }`}
                    disabled={busy}
                    onClick={() => void saveOutcome("rejected")}
                  >
                    Rejected
                  </button>
                </div>
              </>
            )}
          </section>

          {message && <p className={styles.statusNote}>{message}</p>}
          {error && <p className={styles.warningPanel}>{error}</p>}
        </aside>
      </div>
    </main>
  );
}
