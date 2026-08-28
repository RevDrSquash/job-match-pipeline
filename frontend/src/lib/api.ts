import type {
  Generation,
  JobDetail,
  JobSummary,
  Match,
  Profile,
  SkillDetail,
  SkillGraphPayload,
  SkillSearchHit,
  SkillStats,
  User,
} from "@/lib/types";

type JsonRecord = Record<string, unknown>;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(message);
  }

  return (await response.json()) as T;
}

export async function fetchUsers(): Promise<User[]> {
  const result = await request<{ users: User[] }>("/api/users");
  return result.users;
}

export async function fetchMatches(userId: string): Promise<Match[]> {
  const query = new URLSearchParams({ user_id: userId });
  const result = await request<{ matches: Match[] }>(`/api/matches?${query}`);
  return result.matches;
}

export function fetchProfile(userId: string): Promise<Profile> {
  const query = new URLSearchParams({ user_id: userId });
  return request<Profile>(`/api/profile?${query}`);
}

export function updateProfile(body: JsonRecord): Promise<Profile> {
  return request<Profile>("/api/profile", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function recordMatchEvent(
  matchId: string,
  body: JsonRecord,
): Promise<JsonRecord> {
  return request<JsonRecord>(`/api/matches/${matchId}/events`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function requestGeneration(
  matchId: string,
): Promise<{
  action: "enqueued" | "skipped_existing" | "quota_exhausted";
  generation_id: string | null;
}> {
  return request(`/api/matches/${matchId}/generate`, { method: "POST" });
}

export function fetchGeneration(generationId: string): Promise<Generation> {
  return request<Generation>(`/api/generations/${generationId}`);
}

export async function searchJobs(query = ""): Promise<JobSummary[]> {
  const params = new URLSearchParams();
  const trimmed = query.trim();
  if (trimmed) params.set("q", trimmed);
  const suffix = params.size ? `?${params}` : "";
  const result = await request<{ jobs: JobSummary[] }>(`/api/jobs${suffix}`);
  return result.jobs;
}

export function fetchJob(jobId: string): Promise<JobDetail> {
  return request<JobDetail>(`/api/jobs/${jobId}`);
}

export function fetchSkillStats(): Promise<SkillStats> {
  return request<SkillStats>("/api/skills/stats");
}

export async function searchSkills(query: string, limit = 20): Promise<SkillSearchHit[]> {
  const params = new URLSearchParams({ q: query.trim(), limit: String(limit) });
  const result = await request<{ results: SkillSearchHit[] }>(`/api/skills/search?${params}`);
  return result.results;
}

export function fetchSkill(conceptId: string): Promise<SkillDetail> {
  return request<SkillDetail>(`/api/skills/${conceptId}`);
}

export function fetchSkillGraph(
  conceptId: string,
  depth = 1,
  limit = 150,
): Promise<SkillGraphPayload> {
  const params = new URLSearchParams({
    depth: String(depth),
    limit: String(limit),
  });
  return request<SkillGraphPayload>(`/api/skills/${conceptId}/graph?${params}`);
}
