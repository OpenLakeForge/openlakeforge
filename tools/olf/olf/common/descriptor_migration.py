"""`olf project migrate`: descriptors from the previous API version to the current one.

``source.yaml`` goes from v1alpha3 to v1beta1 and ``governance.yml`` from v1beta1
to v1beta2. A Source's fields are the same in both versions, so its migration is
the version line alone, checked by validating the result.

The Deployment Profile (``openlakeforge.yaml``) goes from v1alpha1 to v1beta1: the
version line changes and ``spec.access.issuer`` is renamed ``tls``. Like the
governance rewrite it is textual, so comments survive, and it is refused rather
than guessed at when the result differs from the migration of the parsed data.

For governance, v1beta1 declared products in a root ``products:`` list that named their
domain; v1beta2 nests them under ``domains[].products`` and drops that field.
The rewrite is textual so comments, ordering and untouched formatting survive,
and it is checked against the same migration computed on the parsed data: if
the two disagree the file is refused rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from openlakeforge_core import GOVERNANCE_API_VERSION, GOVERNANCE_KIND, LakehouseDescriptorError
from openlakeforge_core.descriptors import (
    LEGACY_SOURCE_API_VERSION,
    SOURCE_API_VERSION,
    SOURCE_KIND,
    validate_source_descriptor,
)
from openlakeforge_core.governance import validate_governance_descriptor

from olf.common.profile import (
    LEGACY_PROFILE_API_VERSION,
    PROFILE_API_VERSION,
    PROFILE_KIND,
    DeploymentProfileError,
    validate_deployment_profile,
)

PROFILE_FILE = "openlakeforge.yaml"
LEGACY_API_VERSION = "openlakeforge.io/v1beta1"
_ITEM = re.compile(r"^(\s*)-(\s|$)")


class MigrationError(ValueError):
    """The descriptor cannot be migrated; nothing was changed."""


def _parse(text: str, source: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MigrationError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise MigrationError(f"{source}: descriptor must contain a YAML object")
    return document


def _migrated_document(document: dict[str, Any], source: str) -> dict[str, Any]:
    """The v1beta2 document the text migration must produce."""
    if document.get("apiVersion") == GOVERNANCE_API_VERSION:
        raise MigrationError(f"{source}: already {GOVERNANCE_API_VERSION!r}; nothing to migrate")
    if document.get("apiVersion") != LEGACY_API_VERSION:
        raise MigrationError(
            f"{source}: apiVersion {document.get('apiVersion')!r} is not {LEGACY_API_VERSION!r}; "
            "only v1beta1 documents are migrated"
        )
    if document.get("kind") != GOVERNANCE_KIND:
        raise MigrationError(f"{source}: kind must be {GOVERNANCE_KIND!r}")
    domains, products = document.get("domains"), document.get("products", [])
    if not isinstance(domains, list) or not isinstance(products, list):
        raise MigrationError(f"{source}: domains and products must be arrays")
    if any(isinstance(domain, dict) and "products" in domain for domain in domains):
        raise MigrationError(f"{source}: a domain already declares `products`; v1beta1 and v1beta2 layouts are mixed")
    by_name = {d.get("name"): {**d, "products": []} for d in domains if isinstance(d, dict)}
    if len(by_name) != len(domains):
        raise MigrationError(f"{source}: domains must be unique objects with a name")
    seen: set[object] = set()
    for product in products:
        if not isinstance(product, dict):
            raise MigrationError(f"{source}: products must contain objects")
        if product.get("id") in seen:
            raise MigrationError(f"{source}: duplicate product id {product.get('id')!r}")
        seen.add(product.get("id"))
        if product.get("domain") not in by_name:
            raise MigrationError(
                f"{source}: product {product.get('id')!r} names unknown domain {product.get('domain')!r}; "
                "declare the domain or fix the name, then migrate again"
            )
        by_name[product["domain"]]["products"].append({k: v for k, v in product.items() if k != "domain"})
    return {
        "apiVersion": GOVERNANCE_API_VERSION,
        "kind": GOVERNANCE_KIND,
        "domains": [{k: v for k, v in by_name[d["name"]].items() if k != "products" or v} for d in domains],
    }


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_content(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def _span(lines: list[str], key: str, source: str) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{key}:", line)), None)
    if start is None:
        raise MigrationError(f"{source}: missing top-level `{key}:`")
    end = start + 1
    while end < len(lines) and not re.match(r"^[A-Za-z]", lines[end]):
        end += 1
    return start, end


def _entries(lines: list[str], start: int, end: int) -> list[tuple[int, int, int]]:
    """(lead, item, end) of each item of the block list in lines[start + 1:end].

    ``lead`` reaches back over the comment lines directly above the item, so a
    comment travels with the entry it describes."""
    first = next((i for i in range(start + 1, end) if _ITEM.match(lines[i])), None)
    if first is None:
        return []
    indent = _indent(lines[first])
    items = [i for i in range(start + 1, end) if _ITEM.match(lines[i]) and _indent(lines[i]) == indent]
    leads = []
    for n, item in enumerate(items):
        lead = item
        floor = items[n - 1] + 1 if n else start + 1
        while lead > floor and lines[lead - 1].lstrip().startswith("#"):
            lead -= 1
        leads.append(lead)
    return [(leads[n], item, leads[n + 1] if n + 1 < len(items) else end) for n, item in enumerate(items)]


def _without_domain_key(entry: list[str], item_indent: int, lead: int) -> list[str]:
    key_indent = item_indent + 2
    out = list(entry)
    if re.match(r"^\s*-\s+domain:", out[lead]):
        # `- domain: x` shares its line with the item marker; the next key inherits the marker.
        del out[lead]
        following = next((i for i in range(lead, len(out)) if _is_content(out[i])), None)
        if following is None:
            return []
        out[following] = " " * item_indent + "- " + out[following].lstrip()
        return out
    return [
        line
        for i, line in enumerate(out)
        if not (i > lead and _indent(line) == key_indent and re.match(r"\s*domain:", line))
    ]


def is_legacy_governance(text: str) -> bool:
    """Whether the parsed top-level ``apiVersion`` is v1beta1 (the text may mention it anywhere else)."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(document, dict) and document.get("apiVersion") == LEGACY_API_VERSION


def migrate_governance_text(text: str, *, source: str = "governance.yml") -> str:
    """The v1beta2 text for a v1beta1 ``governance.yml``; raises ``MigrationError`` instead of guessing."""
    document = _parse(text, source)
    expected = _migrated_document(document, source)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    products_by_domain: dict[str, list[list[str]]] = {d["name"]: [] for d in document["domains"]}
    if "products" in document:
        p_start, p_end = _span(lines, "products", source)
        if re.match(r"^products:\s*\[.*\]", lines[p_start]):
            if document["products"]:
                raise MigrationError(f"{source}: flow-style `products:` with entries; rewrite it as a block list")
            entries = []
        else:
            entries = _entries(lines, p_start, p_end)
        kept: set[int] = set()
        for (s, item, e), product in zip(entries, document["products"], strict=True):
            body = lines[s:e]
            while body and not _is_content(body[-1]):
                body.pop()
            kept.update(range(s, s + len(body)))
            moved = _without_domain_key(body, _indent(lines[item]), item - s)
            products_by_domain[product["domain"]].append(moved)
        # A comment that belongs to no entry (after the last product, or apart from the first) stays where
        # the section was rather than disappearing with it.
        orphans = [
            lines[i] for i in range(p_start + 1, p_end) if i not in kept and lines[i].lstrip().startswith("#")
        ]
        lines[p_start:p_end] = orphans
        while lines and not lines[-1].strip():
            lines.pop()

    d_start, d_end = _span(lines, "domains", source)
    entries = _entries(lines, d_start, d_end)
    if len(entries) != len(document["domains"]):
        raise MigrationError(f"{source}: domains is not laid out as a block list of mappings")
    for (_lead, item, e), domain in reversed(list(zip(entries, document["domains"], strict=True))):
        groups = products_by_domain[domain["name"]]
        if not groups:
            continue
        s = item
        item_indent = _indent(lines[item])
        at = e
        while at > s + 1 and not _is_content(lines[at - 1]):
            at -= 1
        header = " " * (item_indent + 2) + "products:\n"
        block = [header]
        for product in groups:
            shift = item_indent + 4 - _indent(next(line for line in product if _ITEM.match(line)))
            block += [" " * shift + line if shift > 0 and line.strip() else line for line in product]
        if lines[at - 1] and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines[at:at] = block
    result = "".join(lines)
    result = re.sub(
        rf"^apiVersion:(\s*)([\"']?){re.escape(LEGACY_API_VERSION)}\2",
        rf"apiVersion:\1\2{GOVERNANCE_API_VERSION}\2",
        result,
        count=1,
        flags=re.M,
    )
    if not result.endswith("\n"):
        result += "\n"
    try:
        produced = yaml.safe_load(result)
        validate_governance_descriptor(produced, source=source)
    except (yaml.YAMLError, LakehouseDescriptorError) as exc:
        raise MigrationError(
            f"{source}: the rewrite is not a valid v1beta2 document ({exc}); review it by hand"
        ) from exc
    if produced != expected:
        raise MigrationError(
            f"{source}: its layout is not one the text rewrite can move safely (the result differs from the "
            "parsed migration); nest each product under its domain by hand"
        )
    return result


def is_legacy_source(text: str) -> bool:
    """Whether the parsed top-level ``apiVersion`` is v1alpha3 (the text may mention it anywhere else)."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(document, dict) and document.get("apiVersion") == LEGACY_SOURCE_API_VERSION


def migrate_source_text(text: str, *, source: str = "source.yaml") -> str:
    """The v1beta1 text for a v1alpha3 ``source.yaml``; only the ``apiVersion`` line changes."""
    document = _parse(text, source)
    if document.get("apiVersion") == SOURCE_API_VERSION:
        raise MigrationError(f"{source}: already {SOURCE_API_VERSION!r}; nothing to migrate")
    if document.get("apiVersion") != LEGACY_SOURCE_API_VERSION or document.get("kind") != SOURCE_KIND:
        raise MigrationError(
            f"{source}: expected kind {SOURCE_KIND!r} with apiVersion {LEGACY_SOURCE_API_VERSION!r}, "
            f"found {document.get('kind')!r} with {document.get('apiVersion')!r}"
        )
    result, count = re.subn(
        rf"^apiVersion:(\s*)([\"']?){re.escape(LEGACY_SOURCE_API_VERSION)}\2",
        rf"apiVersion:\1\2{SOURCE_API_VERSION}\2",
        text,
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise MigrationError(f"{source}: could not find the top-level apiVersion line to rewrite")
    try:
        validate_source_descriptor(yaml.safe_load(result), source=source)
    except LakehouseDescriptorError as exc:
        raise MigrationError(f"{source}: not a valid {SOURCE_API_VERSION} Source after migration: {exc}") from exc
    return result


def is_legacy_profile(text: str) -> bool:
    """Whether the parsed top-level ``apiVersion`` is v1alpha1 (the text may mention it anywhere else)."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(document, dict) and document.get("apiVersion") == LEGACY_PROFILE_API_VERSION


def _rename_access_issuer(lines: list[str]) -> list[str]:
    """Rename the ``issuer`` key of the ``access`` mapping to ``tls``, in block or flow style."""
    for at, line in enumerate(lines):
        flow = re.match(r"^(\s+access:\s*\{)(.*)(\}.*)$", line)
        if flow:
            renamed = re.sub(r"(^|[{,]\s*)issuer(\s*:)", r"\1tls\2", flow.group(2))
            return [*lines[:at], flow.group(1) + renamed + flow.group(3) + "\n", *lines[at + 1 :]]
        block = re.match(r"^(\s+)access:\s*(#.*)?$", line)
        if not block:
            continue
        end = at + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > len(block.group(1))):
            end += 1
        for key in range(at + 1, end):
            if re.match(r"^\s+issuer:", lines[key]):
                return [*lines[:key], lines[key].replace("issuer:", "tls:", 1), *lines[key + 1 :]]
    return lines


def migrate_profile_text(text: str, *, source: str = PROFILE_FILE) -> str:
    """The v1beta1 text for a v1alpha1 Deployment Profile; raises ``MigrationError`` instead of guessing."""
    document = _parse(text, source)
    if document.get("apiVersion") == PROFILE_API_VERSION:
        raise MigrationError(f"{source}: already {PROFILE_API_VERSION!r}; nothing to migrate")
    if document.get("apiVersion") != LEGACY_PROFILE_API_VERSION or document.get("kind") != PROFILE_KIND:
        raise MigrationError(
            f"{source}: expected kind {PROFILE_KIND!r} with apiVersion {LEGACY_PROFILE_API_VERSION!r}, "
            f"found {document.get('kind')!r} with {document.get('apiVersion')!r}"
        )
    spec = document.get("spec")
    access = spec.get("access") if isinstance(spec, dict) else None
    expected = {**document, "apiVersion": PROFILE_API_VERSION}
    if isinstance(spec, dict) and isinstance(access, dict) and "issuer" in access:
        if "tls" in access:
            raise MigrationError(f"{source}: spec.access sets both 'tls' and 'issuer'; keep 'tls' only, then migrate")
        renamed = {("tls" if key == "issuer" else key): value for key, value in access.items()}
        expected["spec"] = {**spec, "access": renamed}
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines = _rename_access_issuer(lines)
    result, count = re.subn(
        rf"^apiVersion:(\s*)([\"']?){re.escape(LEGACY_PROFILE_API_VERSION)}\2",
        rf"apiVersion:\1\2{PROFILE_API_VERSION}\2",
        "".join(lines),
        count=1,
        flags=re.M,
    )
    if count != 1:
        raise MigrationError(f"{source}: could not find the top-level apiVersion line to rewrite")
    try:
        produced = yaml.safe_load(result)
        validate_deployment_profile(produced, source=source)
    except (yaml.YAMLError, DeploymentProfileError) as exc:
        raise MigrationError(f"{source}: not a valid {PROFILE_API_VERSION} profile after migration: {exc}") from exc
    if produced != expected:
        raise MigrationError(
            f"{source}: its layout is not one the text rewrite can move safely (the result differs from the "
            "parsed migration); rename spec.access.issuer to tls and set apiVersion by hand"
        )
    return result


@dataclass(frozen=True)
class Migration:
    path: Path
    original: str
    migrated: str


def plan_project_migration(root: Path) -> list[Migration]:
    """The profile at ``root`` and every descriptor under ``root/lakehouse_code`` on a previous version, migrated.

    All of them are migrated in memory first, so one that cannot be leaves the
    project untouched; a descriptor already current is skipped."""
    code = root / "lakehouse_code"
    plan: list[Migration] = []
    profile = root / PROFILE_FILE
    if profile.is_file():
        text = profile.read_text(encoding="utf-8")
        if is_legacy_profile(text):
            plan.append(Migration(profile, text, migrate_profile_text(text, source=PROFILE_FILE)))
    governance = code / "governance.yml"
    if governance.is_file():
        text = governance.read_text(encoding="utf-8")
        if is_legacy_governance(text):
            plan.append(Migration(governance, text, migrate_governance_text(text, source=_label(root, governance))))
    for path in sorted(code.glob("bronze/*/source.yaml")):
        text = path.read_text(encoding="utf-8")
        if is_legacy_source(text):
            plan.append(Migration(path, text, migrate_source_text(text, source=_label(root, path))))
    return plan


def _label(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
