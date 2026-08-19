import type { SkillRef } from "@/lib/types";

/** Display label for a skill ref; falls back for unknown esco:slug seed IDs. */
export function skillDisplayLabel(skill: SkillRef): string {
  if (skill.label !== skill.id) {
    return skill.label;
  }
  if (skill.id.startsWith("esco:")) {
    const raw = skill.id.slice("esco:".length);
    return raw
      .split(/[-_]/)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ");
  }
  return skill.label;
}
