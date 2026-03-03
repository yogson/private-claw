# Memory Domain

## Purpose

Define long-term memory persistence and retrieval using filesystem storage (Markdown plus YAML frontmatter) for v1.

## Owned Components

- `CMP_MEMORY_FILESYSTEM_STORE`
- `CMP_AUTOMATION_SCHEDULER` (maintenance integration)

## Scope

- Persist memory artifacts by category:
  - profile,
  - preferences,
  - projects,
  - tasks,
  - facts,
  - summaries.
- Retrieve relevant context for active agent tasks.
- Apply update, deduplication, and consolidation policies.
- Provide browse/export operations for admin use.
- Build and maintain deterministic indexes for fast non-vector retrieval.

## Retrieval and Indexing Strategy (v1, non-vector)

### Indexing Model

Maintain filesystem-backed index files to support fast candidate selection:

- `runtime/memory_indexes/index_by_type.json`
- `runtime/memory_indexes/index_by_tag.json`
- `runtime/memory_indexes/index_by_entity.json`
- `runtime/memory_indexes/index_by_project.json`
- `runtime/memory_indexes/index_by_recency.json`

Each memory artifact should expose metadata fields used by indexes:
- `memory_id`, `type`, `tags`, `entities`, `priority`, `confidence`, `updated_at`, `last_used_at`.

### Retrieval Pipeline

1. Parse user turn for intent hints, entities, and topical tags.
2. Generate candidates from deterministic indexes (type/tag/entity/project).
3. Score candidates using transparent weighted scoring.
4. Select top-K per category and build compact context block.
5. Inject selected context into prompt and record retrieval audit.

### Baseline Scoring (configurable)

Score example:
- `entity_match_weight * entity_score`
- `tag_match_weight * tag_score`
- `type_match_weight * type_score`
- `recency_weight * recency_decay_score`
- `priority_weight * priority_score`
- `confidence_weight * confidence_score`

Default behavior should prefer:
- exact entity matches,
- recent and high-confidence memories,
- high-priority project/task artifacts for active work sessions.

### Context Injection Policy

- Use category caps to prevent context bloat.
- Suggested default cap per turn:
  - profile: up to 2
  - preferences: up to 3
  - projects/tasks: up to 4
  - facts: up to 3
  - summaries: up to 1
- If cap overflow occurs, keep highest-scored artifacts only.

### Maintenance Jobs

Periodic scheduler jobs should:
- rebuild or incrementally repair indexes,
- deduplicate conflicting memory artifacts,
- decay confidence for stale low-signal entries,
- archive expired/obsolete records.

Index update/rebuild triggers:
- On every memory write/update: perform incremental index update for touched artifacts.
- On startup: run integrity check and trigger rebuild if index files are missing/version-mismatched.
- On checksum mismatch or parse failure: mark index degraded and trigger full rebuild.
- On scheduled reconciliation window: run batch consistency scan against source memory files.

Corruption recovery policy:
- Fall back to direct memory-file scan for retrieval while index is degraded.
- Rebuild indexes from canonical memory artifacts.
- Emit audit event with recovery status and affected index files.

## Inputs

- Memory update intents from orchestrator and scheduler jobs.
- Existing memory files from the configured data root.

## Outputs

- Context bundles for response generation.
- Updated memory artifacts and maintenance logs.
- Retrieval/index audit records for tuning and diagnostics.

## Constraints

- No database backend in v1.
- All persisted files must validate required frontmatter fields.
- Writes must remain path-safe under application data root.
- Retrieval must not depend on vector search infrastructure.

## Risks

- Quality drift from noisy or conflicting memory extraction.
- File growth causing slower retrieval if indexing strategy is weak.

## Done Criteria

- Memory persists and remains readable across restarts.
- Retrieval produces relevant context for repeated user interactions.
- Consolidation reduces duplication and stale artifacts.
- Index-backed retrieval remains deterministic and auditable.

