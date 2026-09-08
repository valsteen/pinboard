import tomllib
from pathlib import Path
from typing import Annotated, Final

import msgspec
import yaml

ROOT: Final = Path(__file__).resolve().parent.parent
PLUGIN_NAME: Final = "pinboard"
PROJECT_VERSION: Final = "0.1.0"
EXPECTED_LICENSE_TEXT: Final = """MIT License

Copyright (c) 2026 Vincent Alsteen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
EXPECTED_SKILLS: Final = frozenset(
    {
        "maintaining-agent-guidance",
        "pinboard",
        "pinboard-deliver",
        "pinboard-intake",
        "repository-readiness",
        "slop-cleanup",
    }
)
EXPECTED_SKILL_DISPLAY_NAMES: Final = {
    "maintaining-agent-guidance": "Maintaining Agent Guidance",
    "pinboard": "Pinboard",
    "pinboard-deliver": "Pinboard: Deliver",
    "pinboard-intake": "Pinboard: Intake",
    "repository-readiness": "Repository Readiness",
    "slop-cleanup": "Slop Cleanup",
}
EXPECTED_ENTRY_POINTS: Final = {
    "pinboard": "pinboard.interfaces.cli:main",
}

type SkillName = Annotated[
    str,
    msgspec.Meta(max_length=64, pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z"),
]
type SkillDescription = Annotated[
    str,
    msgspec.Meta(max_length=1024, pattern=r"\A[^<>]*[^<>\s][^<>]*\z"),
]
type NonBlankText = Annotated[str, msgspec.Meta(pattern=r"\A[\s\S]*\S[\s\S]*\z")]


class PluginAuthor(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    url: str


class PluginInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: str = msgspec.field(name="displayName")
    short_description: str = msgspec.field(name="shortDescription")
    long_description: str = msgspec.field(name="longDescription")
    developer_name: str = msgspec.field(name="developerName")
    category: str
    website_url: str = msgspec.field(name="websiteURL")
    capabilities: tuple[str, ...]
    default_prompt: tuple[str, ...] = msgspec.field(name="defaultPrompt")


class PluginManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    version: str
    description: str
    author: PluginAuthor
    homepage: str
    repository: str
    license: str
    keywords: tuple[str, ...]
    skills: str
    interface: PluginInterface


class ClaudePluginManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    description: str
    version: str
    author: PluginAuthor
    homepage: str
    repository: str
    license: str
    keywords: tuple[str, ...]


class ProjectMetadata(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    version: str
    description: NonBlankText
    readme: str
    license: str
    license_files: tuple[str, ...] = msgspec.field(name="license-files")
    requires_python: str = msgspec.field(name="requires-python")
    dependencies: tuple[str, ...]
    scripts: dict[str, str]


class CodexMarketplaceInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: str = msgspec.field(name="displayName")


class CodexPluginSource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source: str
    path: str


class CodexPluginPolicy(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    installation: str
    authentication: str


class CodexMarketplacePlugin(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    source: CodexPluginSource
    policy: CodexPluginPolicy
    category: str


class CodexMarketplaceManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    interface: CodexMarketplaceInterface
    plugins: tuple[CodexMarketplacePlugin, ...]


class ClaudeMarketplaceOwner(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    url: str


class ClaudeMarketplacePlugin(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    description: str
    source: str


class ClaudeMarketplaceManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    description: str
    owner: ClaudeMarketplaceOwner
    plugins: tuple[ClaudeMarketplacePlugin, ...]


class SkillFrontmatter(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: SkillName
    description: SkillDescription
    license: str | None = None
    allowed_tools: str | tuple[str, ...] | None = msgspec.field(name="allowed-tools", default=None)
    metadata: dict[str, str | int | float | bool | tuple[str, ...] | None] | None = None


class SkillInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: NonBlankText
    short_description: NonBlankText
    default_prompt: NonBlankText


class SkillAgent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    interface: SkillInterface


def decode_skill_frontmatter(text: str, path: Path) -> SkillFrontmatter:
    try:
        return msgspec.convert(yaml.safe_load(text), type=SkillFrontmatter, strict=True)
    except (msgspec.ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"{path}: invalid metadata: {error}") from error


def decode_skill_agent(text: str, path: Path) -> SkillAgent:
    try:
        return msgspec.convert(yaml.safe_load(text), type=SkillAgent, strict=True)
    except (msgspec.ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"{path}: invalid metadata: {error}") from error


def validate_plugin() -> None:
    path = ROOT / ".codex-plugin" / "plugin.json"
    value = msgspec.json.decode(path.read_bytes(), type=PluginManifest)
    cachebuster_prefix = f"{PROJECT_VERSION}+codex."
    cachebuster = value.version.removeprefix(cachebuster_prefix)
    if value.name != PLUGIN_NAME or not value.version.startswith(cachebuster_prefix):
        raise ValueError("plugin manifest name or cachebuster version is invalid")
    if len(cachebuster) != 14 or not cachebuster.isdecimal():
        raise ValueError("plugin manifest cachebuster must be a UTC timestamp")
    if value.skills != "./skills/":
        raise ValueError("plugin manifest identity or skill root is invalid")
    if value.license != "MIT":
        raise ValueError("plugin manifest license must match the repository license")
    if (
        value.interface.display_name,
        value.interface.developer_name,
        value.interface.category,
        value.interface.website_url,
        value.interface.capabilities,
    ) != ("Pinboard", value.author.name, "Productivity", value.repository, ("Interactive", "Write")):
        raise ValueError("plugin interface identity or capabilities are invalid")
    if value.repository != "https://github.com/valsteen/pinboard" or value.homepage != f"{value.repository}#readme":
        raise ValueError("plugin repository or homepage is invalid")


def validate_claude_plugin() -> None:
    path = ROOT / ".claude-plugin" / "plugin.json"
    value = msgspec.json.decode(path.read_bytes(), type=ClaudePluginManifest)
    if (value.name, value.version) != (PLUGIN_NAME, PROJECT_VERSION):
        raise ValueError("Claude plugin manifest identity or version is invalid")
    if value.license != "MIT" or value.author.name != "Vincent Alsteen":
        raise ValueError("Claude plugin author or license is invalid")
    if value.repository != "https://github.com/valsteen/pinboard" or value.homepage != f"{value.repository}#readme":
        raise ValueError("Claude plugin repository or homepage is invalid")
    if not (ROOT / "skills").is_dir():
        raise ValueError("Claude plugin must use the shared repository-root skills directory")


def validate_project_metadata() -> None:
    path = ROOT / "pyproject.toml"
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    try:
        project = msgspec.convert(value["project"], type=ProjectMetadata, strict=True)
    except (KeyError, msgspec.ValidationError) as error:
        raise ValueError(f"{path}: invalid project metadata: {error}") from error
    if (project.name, project.version, project.readme, project.license, project.license_files) != (
        PLUGIN_NAME,
        PROJECT_VERSION,
        "README.md",
        "MIT",
        ("LICENSE",),
    ):
        raise ValueError("distribution identity, readme, or license publication is invalid")
    if project.requires_python != ">=3.14,<3.15":
        raise ValueError("distribution must preserve Python 3.14-only compatibility")
    if project.dependencies != ("msgspec>=0.21.1",):
        raise ValueError("msgspec must remain the sole runtime dependency")
    if project.scripts != EXPECTED_ENTRY_POINTS:
        raise ValueError("pinboard must be the only project entry point to the current engine")


def validate_codex_marketplace() -> None:
    path = ROOT / ".agents" / "plugins" / "marketplace.json"
    value = msgspec.json.decode(path.read_bytes(), type=CodexMarketplaceManifest)
    if len(value.plugins) != 1:
        raise ValueError("Codex marketplace metadata must install the repository-root plugin")
    plugin = value.plugins[0]
    if (
        value.name,
        value.interface.display_name,
        plugin.name,
        plugin.source.source,
        plugin.source.path,
        plugin.policy.installation,
        plugin.policy.authentication,
        plugin.category,
    ) != (PLUGIN_NAME, "Pinboard", PLUGIN_NAME, "local", ".", "AVAILABLE", "ON_INSTALL", "Productivity"):
        raise ValueError("Codex marketplace metadata must install the repository-root plugin")


def validate_claude_marketplace() -> None:
    path = ROOT / ".claude-plugin" / "marketplace.json"
    value = msgspec.json.decode(path.read_bytes(), type=ClaudeMarketplaceManifest)
    if len(value.plugins) != 1:
        raise ValueError("Claude marketplace metadata must install the repository-root plugin")
    plugin = value.plugins[0]
    if (value.name, value.owner.name, plugin.name, plugin.source) != (
        PLUGIN_NAME,
        "Vincent Alsteen",
        PLUGIN_NAME,
        "./",
    ):
        raise ValueError("Claude marketplace metadata must install the repository-root plugin")
    if value.description != plugin.description:
        raise ValueError("Claude marketplace and plugin descriptions must match")


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 5 or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path}: skill frontmatter is missing")
    end = lines.index("---", 1)
    frontmatter = decode_skill_frontmatter("\n".join(lines[1:end]), path)
    if frontmatter.name != path.parent.name:
        raise ValueError(f"{path}: skill name must match its directory")
    agent = path.parent / "agents" / "openai.yaml"
    agent_metadata = decode_skill_agent(agent.read_text(encoding="utf-8"), agent)
    if agent_metadata.interface.display_name != EXPECTED_SKILL_DISPLAY_NAMES[frontmatter.name]:
        raise ValueError(f"{agent}: display name does not match the public skill identity")


def main() -> None:
    if (ROOT / "LICENSE").read_text(encoding="utf-8") != EXPECTED_LICENSE_TEXT:
        raise ValueError("repository license must exactly match the canonical MIT license text")
    validate_plugin()
    validate_claude_plugin()
    validate_project_metadata()
    validate_codex_marketplace()
    validate_claude_marketplace()
    skill_paths = tuple(sorted((ROOT / "skills").glob("*/SKILL.md")))
    if {path.parent.name for path in skill_paths} != EXPECTED_SKILLS:
        raise ValueError(
            "public skills must be exactly maintaining-agent-guidance, pinboard, pinboard-deliver, pinboard-intake, "
            "repository-readiness, and slop-cleanup"
        )
    for path in skill_paths:
        validate_skill(path)
    print(f"validated Codex and Claude plugins, Codex and Claude marketplaces, and {len(skill_paths)} skills")


if __name__ == "__main__":
    main()
