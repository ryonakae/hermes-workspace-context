"""Read-only project skill overlays for routed Hermes sessions."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .runtime import current_workspace


_PATCH_MARKER = "_hermes_workspace_context_skill_patch"
_PROJECT_ONLY_LOOKUP: ContextVar[bool] = ContextVar(
    "hermes_workspace_context_project_only_skill_lookup",
    default=False,
)


def _existing_project_roots() -> list[Path]:
    workspace = current_workspace()
    if workspace is None:
        return []
    return [root for root in workspace.skill_dirs if root.is_dir()]


def _dedupe(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = Path(path).resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8")[:4000]
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            return value.strip().strip("'\"") or None
    return None


def _project_has_skill(name: str) -> bool:
    if not isinstance(name, str) or not name or ":" in name:
        return False
    normalized = name.strip().strip("/")
    for root in _existing_project_roots():
        direct = root / normalized / "SKILL.md"
        if direct.is_file():
            return True
        for skill_md in root.rglob("SKILL.md"):
            try:
                relative_name = str(skill_md.parent.relative_to(root))
            except ValueError:
                continue
            if skill_md.parent.name == normalized or relative_name == normalized:
                return True
            if _frontmatter_name(skill_md) == normalized:
                return True
    return False


def install_skill_patches(
    *,
    skill_utils: Any | None = None,
    prompt_builder: Any | None = None,
    skills_tool: Any | None = None,
) -> None:
    """Install task-local project overlays over Hermes read-only skill APIs."""
    if skill_utils is None:
        from agent import skill_utils as skill_utils
    if prompt_builder is None:
        from agent import prompt_builder as prompt_builder
    if skills_tool is None:
        from tools import skills_tool as skills_tool

    if getattr(skill_utils, _PATCH_MARKER, False):
        return

    original_external = skill_utils.get_external_skills_dirs
    original_all = skill_utils.get_all_skills_dirs
    original_prompt_skills_dir = prompt_builder.get_skills_dir
    original_load_snapshot = prompt_builder._load_skills_snapshot
    original_write_snapshot = prompt_builder._write_skills_snapshot
    original_tool_skills_dir = skills_tool._skills_dir
    original_skill_view = skills_tool.skill_view

    def routed_external_dirs() -> list[Path]:
        roots = _existing_project_roots()
        if not roots:
            return list(original_external())
        if _PROJECT_ONLY_LOOKUP.get():
            return []
        return _dedupe([original_tool_skills_dir(), *original_external()])

    def routed_all_dirs() -> list[Path]:
        roots = _existing_project_roots()
        if not roots:
            return list(original_all())
        if _PROJECT_ONLY_LOOKUP.get():
            return roots
        return _dedupe([*roots, *original_all()])

    def routed_prompt_skills_dir() -> Path:
        roots = _existing_project_roots()
        return roots[0] if roots else original_prompt_skills_dir()

    def routed_tool_skills_dir() -> Path:
        roots = _existing_project_roots()
        return roots[0] if roots else original_tool_skills_dir()

    def routed_load_snapshot(root: Path):
        if _existing_project_roots():
            return None
        return original_load_snapshot(root)

    def routed_write_snapshot(*args: Any, **kwargs: Any):
        if _existing_project_roots():
            return None
        return original_write_snapshot(*args, **kwargs)

    def routed_skill_view(name: str, *args: Any, **kwargs: Any):
        if not _project_has_skill(name):
            return original_skill_view(name, *args, **kwargs)
        token = _PROJECT_ONLY_LOOKUP.set(True)
        try:
            return original_skill_view(name, *args, **kwargs)
        finally:
            _PROJECT_ONLY_LOOKUP.reset(token)

    skill_utils.get_external_skills_dirs = routed_external_dirs
    skill_utils.get_all_skills_dirs = routed_all_dirs
    prompt_builder.get_skills_dir = routed_prompt_skills_dir
    prompt_builder.get_all_skills_dirs = routed_all_dirs
    prompt_builder._load_skills_snapshot = routed_load_snapshot
    prompt_builder._write_skills_snapshot = routed_write_snapshot
    skills_tool._skills_dir = routed_tool_skills_dir
    skills_tool.skill_view = routed_skill_view
    setattr(skill_utils, _PATCH_MARKER, True)
