"""Canonical skill linking (ESCO). All skill-name matching goes through here."""

from app.skills.linker import LinkedSkill, SkillLinker, get_skill_linker

__all__ = ["LinkedSkill", "SkillLinker", "get_skill_linker"]
