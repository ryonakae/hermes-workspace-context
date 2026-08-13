import json
from pathlib import Path
from types import SimpleNamespace

from workspace_context.config import Workspace
from workspace_context.runtime import _CURRENT_WORKSPACE
from workspace_context.skills import install_skill_patches


def write_skill(root: Path, dirname: str, name: str, marker: str) -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {marker}\n---\n\n{marker}\n",
        encoding="utf-8",
    )


def workspace(project: Path) -> Workspace:
    return Workspace(
        name="app",
        cwd=project,
        skill_dirs=(project / ".agents" / "skills",),
        mcp_file=None,
    )


def test_routed_skill_roots_put_project_before_global(tmp_path: Path) -> None:
    project_root = tmp_path / "project" / ".agents" / "skills"
    global_root = tmp_path / "global"
    external_root = tmp_path / "external"
    for root in (project_root, global_root, external_root):
        root.mkdir(parents=True)
    skill_utils = SimpleNamespace(
        get_external_skills_dirs=lambda: [external_root],
        get_all_skills_dirs=lambda: [global_root, external_root],
    )
    prompt_builder = SimpleNamespace(
        get_skills_dir=lambda: global_root,
        get_all_skills_dirs=lambda: [global_root, external_root],
        _load_skills_snapshot=lambda _root: {"should": "not load"},
        _write_skills_snapshot=lambda *_args: "written",
    )
    skills_tool = SimpleNamespace(
        _skills_dir=lambda: global_root,
        skill_view=lambda name, **_kwargs: json.dumps({"name": name}),
        _SKILLS_CACHE={},
    )
    install_skill_patches(
        skill_utils=skill_utils,
        prompt_builder=prompt_builder,
        skills_tool=skills_tool,
    )

    token = _CURRENT_WORKSPACE.set(workspace(tmp_path / "project"))
    try:
        assert skill_utils.get_all_skills_dirs() == [project_root, global_root, external_root]
        assert prompt_builder.get_skills_dir() == project_root
        assert prompt_builder.get_all_skills_dirs() == [project_root, global_root, external_root]
        assert skills_tool._skills_dir() == project_root
        assert prompt_builder._load_skills_snapshot(project_root) is None
        assert prompt_builder._write_skills_snapshot("ignored") is None
    finally:
        _CURRENT_WORKSPACE.reset(token)


def test_routed_skill_roots_do_not_recurse_when_all_dirs_calls_external(tmp_path: Path) -> None:
    project_root = tmp_path / "project" / ".agents" / "skills"
    global_root = tmp_path / "global"
    external_root = tmp_path / "external"
    for root in (project_root, global_root, external_root):
        root.mkdir(parents=True)

    skill_utils = SimpleNamespace(get_external_skills_dirs=lambda: [external_root])
    skill_utils.get_all_skills_dirs = lambda: [
        global_root,
        *skill_utils.get_external_skills_dirs(),
    ]
    prompt_builder = SimpleNamespace(
        get_skills_dir=lambda: global_root,
        get_all_skills_dirs=skill_utils.get_all_skills_dirs,
        _load_skills_snapshot=lambda _root: None,
        _write_skills_snapshot=lambda *_args: None,
    )
    skills_tool = SimpleNamespace(
        _skills_dir=lambda: global_root,
        skill_view=lambda name, **_kwargs: json.dumps({"name": name}),
    )
    install_skill_patches(
        skill_utils=skill_utils,
        prompt_builder=prompt_builder,
        skills_tool=skills_tool,
    )

    token = _CURRENT_WORKSPACE.set(workspace(tmp_path / "project"))
    try:
        assert skill_utils.get_all_skills_dirs() == [project_root, global_root, external_root]
    finally:
        _CURRENT_WORKSPACE.reset(token)


def test_unrouted_skill_roots_keep_original_behavior(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    external_root = tmp_path / "external"
    skill_utils = SimpleNamespace(
        get_external_skills_dirs=lambda: [external_root],
        get_all_skills_dirs=lambda: [global_root, external_root],
    )
    prompt_builder = SimpleNamespace(
        get_skills_dir=lambda: global_root,
        get_all_skills_dirs=lambda: [global_root, external_root],
        _load_skills_snapshot=lambda _root: {"global": True},
        _write_skills_snapshot=lambda *_args: "written",
    )
    skills_tool = SimpleNamespace(
        _skills_dir=lambda: global_root,
        skill_view=lambda name, **_kwargs: json.dumps({"name": name}),
        _SKILLS_CACHE={},
    )
    install_skill_patches(
        skill_utils=skill_utils,
        prompt_builder=prompt_builder,
        skills_tool=skills_tool,
    )

    assert skill_utils.get_all_skills_dirs() == [global_root, external_root]
    assert prompt_builder.get_skills_dir() == global_root
    assert prompt_builder._load_skills_snapshot(global_root) == {"global": True}
    assert prompt_builder._write_skills_snapshot("global") == "written"
    assert skills_tool._skills_dir() == global_root


def test_project_skill_shadows_same_named_global_skill_for_view(tmp_path: Path) -> None:
    project_root = tmp_path / "project" / ".agents" / "skills"
    global_root = tmp_path / "global"
    project_root.mkdir(parents=True)
    global_root.mkdir()
    write_skill(project_root, "project-dogfood", "dogfood", "project marker")
    write_skill(global_root, "global-dogfood", "dogfood", "global marker")

    skill_utils = SimpleNamespace(
        get_external_skills_dirs=lambda: [],
        get_all_skills_dirs=lambda: [global_root],
    )
    observed_roots: list[list[Path]] = []

    def fake_skill_view(name: str, **_kwargs):
        observed_roots.append([skills_tool._skills_dir(), *skill_utils.get_external_skills_dirs()])
        return json.dumps({"name": name})

    skills_tool = SimpleNamespace(
        _skills_dir=lambda: global_root,
        skill_view=fake_skill_view,
        _SKILLS_CACHE={},
    )
    prompt_builder = SimpleNamespace(
        get_skills_dir=lambda: global_root,
        get_all_skills_dirs=lambda: [global_root],
        _load_skills_snapshot=lambda _root: None,
        _write_skills_snapshot=lambda *_args: None,
    )
    install_skill_patches(
        skill_utils=skill_utils,
        prompt_builder=prompt_builder,
        skills_tool=skills_tool,
    )

    token = _CURRENT_WORKSPACE.set(workspace(tmp_path / "project"))
    try:
        skills_tool.skill_view("dogfood")
    finally:
        _CURRENT_WORKSPACE.reset(token)

    assert observed_roots == [[project_root]]


def test_non_shadowed_global_skill_remains_visible_in_routed_view(tmp_path: Path) -> None:
    project_root = tmp_path / "project" / ".agents" / "skills"
    global_root = tmp_path / "global"
    project_root.mkdir(parents=True)
    global_root.mkdir()
    write_skill(global_root, "global-only", "global-only", "global marker")

    skill_utils = SimpleNamespace(
        get_external_skills_dirs=lambda: [],
        get_all_skills_dirs=lambda: [global_root],
    )
    observed_roots: list[list[Path]] = []
    skills_tool = SimpleNamespace(_skills_dir=lambda: global_root, _SKILLS_CACHE={})

    def fake_skill_view(name: str, **_kwargs):
        observed_roots.append([skills_tool._skills_dir(), *skill_utils.get_external_skills_dirs()])
        return json.dumps({"name": name})

    skills_tool.skill_view = fake_skill_view
    prompt_builder = SimpleNamespace(
        get_skills_dir=lambda: global_root,
        get_all_skills_dirs=lambda: [global_root],
        _load_skills_snapshot=lambda _root: None,
        _write_skills_snapshot=lambda *_args: None,
    )
    install_skill_patches(
        skill_utils=skill_utils,
        prompt_builder=prompt_builder,
        skills_tool=skills_tool,
    )

    token = _CURRENT_WORKSPACE.set(workspace(tmp_path / "project"))
    try:
        skills_tool.skill_view("global-only")
    finally:
        _CURRENT_WORKSPACE.reset(token)

    assert observed_roots == [[project_root, global_root]]
