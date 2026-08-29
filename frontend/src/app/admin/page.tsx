import type { Metadata } from "next";

import styles from "@/app/dashboard.module.css";
import AdminJobControls from "@/app/ui/admin-job-controls";
import type { AdminMetrics } from "@/lib/types";

export const metadata: Metadata = {
  title: "Admin metrics · Job Match",
};

const apiBaseUrl = (process.env.API_BASE_URL ?? "http://localhost:8080").replace(
  /\/$/,
  "",
);

const FUNNEL_STAGES = [
  ["jobs_ingested", "Jobs ingested"],
  ["prefilter_pairs_peak", "Prefiltered"],
  ["jobs_extracted", "Jobs extracted"],
  ["matches_written_peak", "Reranked"],
  ["screened", "Screened"],
  ["generated", "Generated"],
  ["applied", "Applied"],
] as const;

function percent(value: number | null) {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function dollars(value: number | null | undefined, digits = 4) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value ?? 0);
}

async function getMetrics(): Promise<AdminMetrics | null> {
  try {
    const response = await fetch(`${apiBaseUrl}/api/admin/metrics`, {
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as AdminMetrics;
  } catch {
    return null;
  }
}

export default async function AdminPage() {
  const metrics = await getMetrics();

  if (!metrics) {
    return (
      <main className={styles.shell}>
        <div className={styles.pageHeader}>
          <div>
            <p className={styles.eyebrow}>Pipeline health</p>
            <h1>See where candidates move—and where they stop.</h1>
            <p>
              Trigger ingest and matching jobs here. Metrics will appear once the
              API is reachable.
            </p>
          </div>
        </div>
        <AdminJobControls />
        <div className={styles.errorState}>
          <div>
            <span className={styles.emptyIcon}>!</span>
            <h2>Metrics are unavailable</h2>
            <p>
              The API did not return an admin snapshot. Check the local services,
              then reload this page.
            </p>
          </div>
        </div>
      </main>
    );
  }

  const maxFunnelValue = Math.max(
    1,
    ...FUNNEL_STAGES.map(([key]) => metrics.funnel[key] ?? 0),
  );
  const usageRows = Object.entries(metrics.usage_by_stage).sort(
    ([left], [right]) => left.localeCompare(right),
  );

  return (
    <main className={styles.shell}>
      <div className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>Pipeline health</p>
          <h1>See where candidates move—and where they stop.</h1>
          <p>
            A server-rendered snapshot of corpus coverage, screen-label
            distribution, funnel volume, and recorded LLM spend.
          </p>
        </div>
        <span className={styles.timestamp}>
          Collected {new Date(metrics.collected_at).toLocaleString("en-US")}
        </span>
      </div>

      <AdminJobControls />

      <section className={styles.metricsHero} aria-label="Key metrics">
        <article className={styles.metricCard}>
          <span>Extraction coverage</span>
          <strong>{percent(metrics.extraction_coverage)}</strong>
          <small>Share of ingested jobs with structured extraction</small>
        </article>
        <article className={styles.metricCard}>
          <span>Clearly qualified</span>
          <strong>
            {metrics.label_distribution?.clearly_qualified?.toLocaleString() ?? "0"}
          </strong>
          <small>Matches labeled clearly qualified</small>
        </article>
        <article className={styles.metricCard}>
          <span>LLM spend recorded</span>
          <strong>{dollars(metrics.llm_spend_usd, 3)}</strong>
          <small>Summed from pipeline event cost details</small>
        </article>
      </section>

      <div className={styles.adminGrid}>
        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <h2>Matching funnel</h2>
              <span>Counts are stage snapshots, not a strict cohort.</span>
            </div>
          </div>
          <div className={styles.panelBody}>
            <div className={styles.funnel}>
              {FUNNEL_STAGES.map(([key, label]) => {
                const value = metrics.funnel[key] ?? 0;
                return (
                  <div className={styles.funnelRow} key={key}>
                    <span className={styles.funnelLabel}>{label}</span>
                    <div className={styles.funnelTrack} aria-hidden="true">
                      <div
                        className={styles.funnelFill}
                        style={{ width: `${(value / maxFunnelValue) * 100}%` }}
                      />
                    </div>
                    <span className={styles.funnelValue}>
                      {value.toLocaleString()}
                    </span>
                  </div>
                );
              })}
              {Object.entries(metrics.label_distribution ?? {}).map(([key, value]) => (
                <div className={styles.funnelRow} key={key}>
                  <span className={styles.funnelLabel}>
                    {key.replaceAll("_", " ")}
                  </span>
                  <div className={styles.funnelTrack} aria-hidden="true">
                    <div
                      className={styles.funnelFill}
                      style={{ width: `${(value / maxFunnelValue) * 100}%` }}
                    />
                  </div>
                  <span className={styles.funnelValue}>
                    {value.toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <h2>LLM usage by stage</h2>
              <span>Calls with recorded token and cost details.</span>
            </div>
          </div>
          <div className={styles.panelBody}>
            {usageRows.length ? (
              <div className={styles.usageList}>
                {usageRows.map(([stage, usage]) => (
                  <div className={styles.usageRow} key={stage}>
                    <div>
                      <strong>{stage.replaceAll("-", " ")}</strong>
                      <span>{usage.n ?? 0} recorded calls</span>
                    </div>
                    <span className={styles.usageCost}>
                      {dollars(usage.cost_usd_total)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className={styles.fieldHint}>
                No LLM usage has been recorded in this snapshot.
              </p>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
