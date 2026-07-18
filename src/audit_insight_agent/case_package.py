"""Discovery and validation of self-contained synthetic audit case packages."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import load_source_catalog
from .models import AuditRule, RelationshipCatalog, RuleCatalog, SourceCatalog


@dataclass(frozen=True, slots=True)
class CasePackage:
    name: str
    root: Path
    sources: SourceCatalog
    relationships: RelationshipCatalog
    rules: tuple[AuditRule, ...]
    prompts_dir: Path


def _yaml_mapping(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return raw


def load_rule_files(paths: list[Path]) -> tuple[AuditRule, ...]:
    rules = []
    for directory in paths:
        if not directory.exists():
            continue
        for path in sorted([*directory.rglob("*.yaml"), *directory.rglob("*.yml")]):
            raw = _yaml_mapping(path)
            if "rules" in raw:
                rules.extend(RuleCatalog.model_validate(raw).rules)
            else:
                rules.append(AuditRule.model_validate(raw))
    identifiers = [rule.rule_id for rule in rules]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("rule_id values must be unique across the selected packages")
    return tuple(rules)


def load_case_package(
    case_dir: str | Path,
    shared_rules_dir: str | Path | None = None,
) -> CasePackage:
    root = Path(case_dir).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Case package not found: {root}")
    sources_path = root / "data_sources.yaml"
    relationships_path = root / "relationships.yaml"
    sources = load_source_catalog(sources_path)
    relationships = (
        RelationshipCatalog.model_validate(_yaml_mapping(relationships_path))
        if relationships_path.exists()
        else RelationshipCatalog()
    )
    rule_dirs = [root / "rules"]
    if shared_rules_dir is not None:
        rule_dirs.insert(0, Path(shared_rules_dir).expanduser().resolve())
    rules = load_rule_files(rule_dirs)

    source_ids = {source.source_id for source in sources.sources}
    for rule in rules:
        unknown = set(rule.source_ids) - source_ids
        if unknown:
            raise ValueError(f"Rule {rule.rule_id} references unknown sources: {sorted(unknown)}")
    for relationship in relationships.relationships:
        unknown = {relationship.left_source, relationship.right_source} - source_ids
        if unknown:
            raise ValueError(
                f"Relationship {relationship.relationship_id} references unknown sources: {sorted(unknown)}"
            )
    return CasePackage(
        name=root.name,
        root=root,
        sources=sources,
        relationships=relationships,
        rules=rules,
        prompts_dir=root / "prompts",
    )


def select_relevant_rules(query: str, rules: tuple[AuditRule, ...]) -> tuple[AuditRule, ...]:
    """Select checks by query vocabulary; fall back to all enabled checks."""

    enabled = tuple(rule for rule in rules if rule.enabled)
    ignored_prefixes = {"аудит", "прове", "check", "revie", "тольк", "only"}
    raw_tokens = set(re.findall(r"[\w-]{3,}", query.casefold()))
    restrict_to_best_match = bool({"только", "only"} & raw_tokens)
    tokens = {
        token
        for token in raw_tokens
        if token[:5] not in ignored_prefixes
    }
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
    return tuple(rule for _, rule in sorted(scored, key=lambda item: (-item[0], item[1].rule_id)))
