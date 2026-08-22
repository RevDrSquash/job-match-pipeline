import type { Metadata } from "next";

import JobSearch from "@/app/ui/job-search";

export const metadata: Metadata = {
  title: "Jobs · Job Match",
};

export default function JobsPage() {
  return <JobSearch />;
}
