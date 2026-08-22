import type { Metadata } from "next";
import { notFound } from "next/navigation";

import JobDetail from "@/app/ui/job-detail";
import type { JobDetail as JobDetailPayload } from "@/lib/types";

export const metadata: Metadata = {
  title: "Job description · Job Match",
};

const apiBaseUrl = (process.env.API_BASE_URL ?? "http://localhost:8080").replace(
  /\/$/,
  "",
);

export default async function JobPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const response = await fetch(`${apiBaseUrl}/api/jobs/${id}`, {
    cache: "no-store",
  });

  if (response.status === 404) notFound();
  if (!response.ok) {
    throw new Error(`Job could not be loaded (${response.status}).`);
  }

  const job = (await response.json()) as JobDetailPayload;
  return <JobDetail job={job} />;
}
