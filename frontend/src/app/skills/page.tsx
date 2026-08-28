import type { Metadata } from "next";
import { Suspense } from "react";

import styles from "@/app/skills.module.css";
import SkillGraphExplorer from "@/app/ui/skill-graph";
import type { SkillStats } from "@/lib/types";

export const metadata: Metadata = {
  title: "Skills · Job Match",
};

const apiBaseUrl = (process.env.API_BASE_URL ?? "http://localhost:8080").replace(
  /\/$/,
  "",
);

async function getStats(): Promise<SkillStats | null> {
  try {
    const response = await fetch(`${apiBaseUrl}/api/skills/stats`, {
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as SkillStats;
  } catch {
    return null;
  }
}

export default async function SkillsPage({
  searchParams,
}: {
  searchParams: Promise<{ concept?: string | string[] }>;
}) {
  const params = await searchParams;
  const raw = params.concept;
  const initialConceptId = Array.isArray(raw) ? raw[0] : (raw ?? "");
  const stats = await getStats();

  return (
    <Suspense
      fallback={
        <main className={styles.shell}>
          <div className={styles.canvasLoading}>Loading skill explorer…</div>
        </main>
      }
    >
      <SkillGraphExplorer initialConceptId={initialConceptId} stats={stats} />
    </Suspense>
  );
}
