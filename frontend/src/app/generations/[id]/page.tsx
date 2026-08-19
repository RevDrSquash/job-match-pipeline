import type { Metadata } from "next";
import { notFound } from "next/navigation";

import Handoff from "@/app/ui/handoff";
import type { Generation } from "@/lib/types";

export const metadata: Metadata = {
  title: "Application handoff · Job Match",
};

const apiBaseUrl = (process.env.API_BASE_URL ?? "http://localhost:8080").replace(
  /\/$/,
  "",
);

export default async function GenerationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const response = await fetch(`${apiBaseUrl}/api/generations/${id}`, {
    cache: "no-store",
  });

  if (response.status === 404) notFound();
  if (!response.ok) {
    throw new Error(`Generation could not be loaded (${response.status}).`);
  }

  const generation = (await response.json()) as Generation;
  return <Handoff generation={generation} />;
}
