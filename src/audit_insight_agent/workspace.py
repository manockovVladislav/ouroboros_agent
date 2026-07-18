"""Automatic discovery of audit inputs and applicable declarative checks."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import load_source_catalog, resolve_source_location
from .data_loader import DATABASE_FORMATS, SUPPORTED_EXTENSIONS
from .document_loader import DOCUMENT_FORMATS
from .models import (
    AuditRule,
    RelationshipCatalog,
    RelationshipConfig,
    RuleCatalog,
    SourceCatalog,
    SourceConfig,
)


@dataclass(frozen=True, slots=True)
class AuditWorkspace:
    """Runtime description assembled from data/, knowledge/ and rules/."""

    project_root: Path
    data_root: Path
    knowledge_root: Path
    rules_root: Path
    source_config_path: Path
    sources: SourceCatalog
    relationships: RelationshipCatalog
    rules: tuple[AuditRule, ...]
    skipped_rules: dict[str, list[str]]


def _yaml_mapping(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return raw


def _source_id(path: Path, root: Path, used: set[str]) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("_.-").lower()
    if not stem or not re.fullmatch(r"[A-Za-z0-9_.-]+", stem):
        stem = "source"
    candidate = stem
    if candidate in used:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        suffix = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:10]
        candidate = f"{stem}_{suffix}"
    used.add(candidate)
    return candidate


def _discovered_sources(
    data_root: Path, knowledge_root: Path
) -> list[SourceConfig]:
    sources: list[SourceConfig] = []
    used: set[str] = set()
    roots = ((data_root, "data"), (knowledge_root, "knowledge"))
    for root, origin in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "private" in {
                part.casefold() for part in path.relative_to(root).parts
            }:
                continue
            extension = path.suffix.casefold()
            if origin == "data" and extension in SUPPORTED_EXTENSIONS:
                source_type = "table"
            elif extension in DOCUMENT_FORMATS:
                source_type = "document"
            else:
                continue
            source_id = _source_id(path, root, used)
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            sources.append(
                SourceConfig(
                    source_id=source_id,
                    source_type=source_type,
                    location=str(path.resolve()),
                    metadata={"origin": origin, "relative_path": relative},
                )
            )
    return sources


def _configured_sources(
    config_path: Path,
    data_root: Path,
    knowledge_root: Path,
) -> list[SourceConfig]:
    if not config_path.is_file():
        return []
    configured = []
    for source in load_source_catalog(config_path).sources:
        if source.format.casefold() in DATABASE_FORMATS:
            configured.append(source)
            continue
        path = resolve_source_location(source, config_path)
        if not any(
            path == root or root in path.parents
            for root in (data_root.resolve(), knowledge_root.resolve())
        ):
            raise PermissionError(
                f"Configured source must stay inside data/ or knowledge/: {path}"
            )
        configured.append(source.model_copy(update={"location": str(path)}))
    return configured


def _merge_sources(
    discovered: list[SourceConfig], configured: list[SourceConfig]
) -> SourceCatalog:
    by_location = {
        Path(source.location).resolve(): index
        for index, source in enumerate(discovered)
        if source.location and source.format.casefold() not in DATABASE_FORMATS
    }
    result = list(discovered)
    used_ids = {source.source_id for source in result}
    for source in configured:
        if source.format.casefold() in DATABASE_FORMATS:
            if source.source_id in used_ids:
                raise ValueError(f"Duplicate configured source_id: {source.source_id}")
            result.append(source)
            used_ids.add(source.source_id)
            continue
        index = by_location.get(Path(source.location).resolve())
        if index is not None:
            discovered_id = result[index].source_id
            replacement = source
            if source.source_id in used_ids and source.source_id != discovered_id:
                raise ValueError(f"Duplicate configured source_id: {source.source_id}")
            used_ids.discard(discovered_id)
            used_ids.add(source.source_id)
            result[index] = replacement
            continue
        if source.source_id in used_ids:
            raise ValueError(f"Duplicate configured source_id: {source.source_id}")
        result.append(source)
        used_ids.add(source.source_id)
    return SourceCatalog(sources=result)


def load_rule_files(rules_root: Path) -> tuple[AuditRule, ...]:
    rules: list[AuditRule] = []
    if not rules_root.is_dir():
        return ()
    for path in sorted([*rules_root.rglob("*.yaml"), *rules_root.rglob("*.yml")]):
        raw = _yaml_mapping(path)
        if "relationships" in raw:
            continue
        if "rules" in raw:
            rules.extend(RuleCatalog.model_validate(raw).rules)
        elif "rule_id" in raw:
            rules.append(AuditRule.model_validate(raw))
    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("rule_id values must be unique")
    return tuple(rules)


def load_relationship_files(rules_root: Path) -> RelationshipCatalog:
    relationships: list[RelationshipConfig] = []
    if rules_root.is_dir():
        for path in sorted([*rules_root.rglob("*.yaml"), *rules_root.rglob("*.yml")]):
            raw = _yaml_mapping(path)
            if "relationships" in raw:
                relationships.extend(
                    RelationshipCatalog.model_validate(raw).relationships
                )
    identifiers = [item.relationship_id for item in relationships]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("relationship_id values must be unique")
    return RelationshipCatalog(relationships=relationships)


def discover_workspace(
    project_root: str | Path,
    *,
    data_root: str | Path | None = None,
    knowledge_root: str | Path | None = None,
    rules_root: str | Path | None = None,
    source_config_path: str | Path | None = None,
) -> AuditWorkspace:
    """Build a validated runtime package without a preselected case."""

    project = Path(project_root).expanduser().resolve()
    data = Path(data_root or project / "data").expanduser().resolve()
    knowledge = Path(knowledge_root or project / "knowledge").expanduser().resolve()
    rule_dir = Path(rules_root or project / "rules").expanduser().resolve()
    config = Path(
        source_config_path or project / "configs" / "data_sources.yaml"
    ).expanduser().resolve()
    if not data.is_dir():
        raise NotADirectoryError(f"Data directory not found: {data}")

    sources = _merge_sources(
        _discovered_sources(data, knowledge),
        _configured_sources(config, data, knowledge),
    )
    if not sources.sources:
        raise ValueError("В data/ и knowledge/ не найдено поддерживаемых источников")
    source_ids = {source.source_id for source in sources.sources if source.enabled}
    all_rules = load_rule_files(rule_dir)
    skipped_rules: dict[str, list[str]] = {}
    applicable_rules = []
    for rule in all_rules:
        missing = sorted(set(rule.source_ids) - source_ids)
        if missing:
            skipped_rules[rule.rule_id] = missing
        else:
            applicable_rules.append(rule)
    relationships = load_relationship_files(rule_dir)
    applicable_relationships = [
        item
        for item in relationships.relationships
        if {item.left_source, item.right_source} <= source_ids
    ]
    return AuditWorkspace(
        project_root=project,
        data_root=data,
        knowledge_root=knowledge,
        rules_root=rule_dir,
        source_config_path=config,
        sources=sources,
        relationships=RelationshipCatalog(relationships=applicable_relationships),
        rules=tuple(applicable_rules),
        skipped_rules=skipped_rules,
    )


def select_relevant_rules(
    query: str, rules: tuple[AuditRule, ...]
) -> tuple[AuditRule, ...]:
    """Select checks by query vocabulary; fall back to all enabled checks."""

    enabled = tuple(rule for rule in rules if rule.enabled)
    ignored_prefixes = {"аудит", "прове", "check", "revie", "тольк", "only"}
    raw_tokens = set(re.findall(r"[\w-]{3,}", query.casefold()))
    restrict_to_best_match = bool({"только", "only"} & raw_tokens)
    tokens = {token for token in raw_tokens if token[:5] not in ignored_prefixes}
    if not tokens:
        return enabled
    scored = []
    for rule in enabled:
        searchable = " ".join(
            [rule.rule_id, rule.description, *rule.tags, *rule.source_ids]
        ).casefold()
        searchable_tokens = set(re.findall(r"[\w-]{3,}", searchable))
        score = sum(
            any(
                token in candidate
                or candidate in token
                or (
                    len(token) >= 5
                    and len(candidate) >= 5
                    and token[:5] == candidate[:5]
                )
                for candidate in searchable_tokens
            )
            for token in tokens
        )
        if score:
            scored.append((score, rule))
    if not scored:
        return enabled
    if restrict_to_best_match:
        best_score = max(score for score, _ in scored)
        scored = [item for item in scored if item[0] == best_score]
    return tuple(
        rule
        for _, rule in sorted(scored, key=lambda item: (-item[0], item[1].rule_id))
    )
