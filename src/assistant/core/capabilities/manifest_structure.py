"""
Component ID: CMP_CORE_CAPABILITIES

Structural checks of an agent-edited capability manifest on the YAML node tree.

Text matching cannot protect the ``self_edit`` opt-in: YAML decodes escapes
(``"self\\x5fedit"``) and merge keys (``<<: {...}``) into keys that never appear
literally in the text, and duplicate keys silently resolve to the last value. The
checks here therefore run on ``yaml.compose`` output, where scalar keys are already
unescaped:

- exactly one YAML document whose root is a mapping;
- no merge keys (``<<``) and no aliases anywhere in the document;
- every mapping key is a scalar, and no mapping has two keys that are equal after
  NFKC + casefold normalisation (so ``self_edit`` and ``Self_Edit`` collide);
- top-level keys that only *normalise* to a protected key must be spelled exactly;
- exactly one top-level ``self_edit`` and exactly one ``capability_id`` key;
- ``self_edit`` is the plain YAML boolean ``true`` (not quoted, not ``yes``/``on``/``1``)
  and ``capability_id`` is a plain string scalar.
"""

import unicodedata
from dataclasses import dataclass

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

SELF_EDIT_KEY = "self_edit"
CAPABILITY_ID_KEY = "capability_id"
PROTECTED_KEYS = (SELF_EDIT_KEY, CAPABILITY_ID_KEY)

_MERGE_TAG = "tag:yaml.org,2002:merge"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_STR_TAG = "tag:yaml.org,2002:str"
_TRUE_SPELLINGS = frozenset({"true", "True", "TRUE"})


class ManifestStructureError(Exception):
    """The manifest's YAML structure is not acceptable for an agent edit."""


@dataclass(frozen=True)
class ManifestStructure:
    """Facts of a structurally valid manifest that the edit guard compares."""

    top_level_keys: tuple[str, ...]
    self_edit: str  # raw scalar text of the self_edit value (always a ``true`` spelling)
    capability_id: str


def normalise_key(key: str) -> str:
    """Comparison form of a mapping key (NFKC, casefolded)."""
    return unicodedata.normalize("NFKC", key).casefold()


def check_manifest_structure(text: str) -> ManifestStructure:
    """Validate ``text`` structurally; raise :class:`ManifestStructureError` if refused."""
    root = _compose_single(text)
    if not isinstance(root, MappingNode):
        raise ManifestStructureError("the manifest must be a YAML mapping")
    _walk(root, seen=set())
    values = _top_level(root)
    self_edit = values.get(SELF_EDIT_KEY)
    if (
        not isinstance(self_edit, ScalarNode)
        or self_edit.tag != _BOOL_TAG
        or self_edit.style is not None
        or self_edit.value not in _TRUE_SPELLINGS
    ):
        raise ManifestStructureError(
            "self_edit must be the plain YAML boolean true; only a human may change it"
        )
    capability_id = values.get(CAPABILITY_ID_KEY)
    if not isinstance(capability_id, ScalarNode) or capability_id.tag != _STR_TAG:
        raise ManifestStructureError("capability_id must be a plain string")
    return ManifestStructure(
        top_level_keys=tuple(values),
        self_edit=self_edit.value,
        capability_id=capability_id.value,
    )


def _compose_single(text: str) -> Node | None:
    try:
        node: Node | None = yaml.compose(text, Loader=yaml.SafeLoader)
        return node
    except yaml.YAMLError as exc:
        raise ManifestStructureError(f"YAML parse error: {exc}") from exc


def _walk(node: Node, seen: set[int]) -> None:
    """Reject aliases, merge keys, non-scalar keys and duplicate keys at every level."""
    # The composer resolves an alias to the anchored node itself, so a node reached
    # twice is an alias.
    if id(node) in seen:
        raise ManifestStructureError("YAML aliases (*name) are not allowed in editable manifests")
    seen.add(id(node))
    if isinstance(node, SequenceNode):
        for item in node.value:
            _walk(item, seen)
    elif isinstance(node, MappingNode):
        keys: dict[str, str] = {}
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG or (
                isinstance(key_node, ScalarNode) and key_node.value == "<<"
            ):
                raise ManifestStructureError(
                    "YAML merge keys (<<) are not allowed in editable manifests"
                )
            if not isinstance(key_node, ScalarNode):
                raise ManifestStructureError("mapping keys must be plain scalars")
            _walk(key_node, seen)
            normalised = normalise_key(key_node.value)
            if normalised in keys:
                raise ManifestStructureError(
                    f"duplicate key {key_node.value!r} (collides with {keys[normalised]!r})"
                )
            keys[normalised] = key_node.value
            _walk(value_node, seen)


def _top_level(root: MappingNode) -> dict[str, Node]:
    values: dict[str, Node] = {}
    counts = dict.fromkeys(PROTECTED_KEYS, 0)
    for key_node, value_node in root.value:
        key = key_node.value
        for protected in PROTECTED_KEYS:
            if key == protected:
                counts[protected] += 1
            elif normalise_key(key) == protected:
                raise ManifestStructureError(
                    f"top-level key {key!r} imitates {protected!r}; only a human may set it"
                )
        values[key] = value_node
    for protected, count in counts.items():
        if count != 1:
            raise ManifestStructureError(
                f"the manifest must contain exactly one top-level {protected!r} key "
                f"(found {count}); only a human may add, remove or duplicate it"
            )
    return values
