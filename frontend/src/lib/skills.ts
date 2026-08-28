import type { SkillRef } from "@/lib/types";

const CONCEPT_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True when a stored skill id is a canonical concept UUID (not seed:/esco:). */
export function isCanonicalConceptId(id: string): boolean {
  return CONCEPT_UUID.test(id);
}

/** Display label for a skill ref; falls back for unknown seed:<slug> IDs. */
export function skillDisplayLabel(skill: SkillRef): string {
  if (skill.label !== skill.id) {
    return skill.label;
  }
  for (const prefix of ["seed:", "esco:"] as const) {
    if (skill.id.startsWith(prefix)) {
      const raw = skill.id.slice(prefix.length);
      return raw
        .split(/[-_]/)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
    }
  }
  return skill.label;
}
