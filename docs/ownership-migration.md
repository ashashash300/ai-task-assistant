# SQLite Ownership Migration

## Status

**Do not apply this migration to a real application database without a reviewed maintenance plan.** The authenticated owner-scoped ORM, services, routes, tools, and tests are now implemented, but this repository does not run migration automatically.

The migration tool exists only for isolated dry runs and automated test rehearsal until the production cutover prerequisites are complete.

## Legacy Schema

The pre-cutover tables have no owner column:

- `tasks` has task fields and timestamps.
- `conversations` has a globally unique `conversation_id`, provider state, `state_version`, and timestamps.
- `users` and `sessions` may already exist, but are not connected to legacy Task or Conversation rows.

`Base.metadata.create_all()` creates missing tables. It is not a migration system and cannot safely add ownership columns or replace the global conversation uniqueness constraint.

## Target Schema

The active target schema is:

- `tasks.user_id` is `INTEGER NOT NULL REFERENCES users(id)`.
- `tasks` has indexes on `(user_id, id)` and `(user_id, due_date)` for owner-scoped lookup and filtering.
- `conversations.user_id` is `INTEGER NOT NULL REFERENCES users(id)`.
- `conversations` uses `UNIQUE(user_id, conversation_id)` instead of global `conversation_id` uniqueness.
- `conversations` retains `state_version`, `updated_at`, and a non-reused SQLite row ID for optimistic locking, retention, and ABA protection.

The migration intentionally renames current rows to `legacy_tasks` and `legacy_conversations`. It creates new, empty active `tasks` and `conversations` tables. No legacy Task or Conversation is assigned to a user automatically.

## Dry Run

The script requires an explicit, existing **absolute** SQLite file path. It rejects omitted, relative, URI-style, and in-memory paths. Dry-run is the default and only validates schema, integrity, foreign keys, and row counts.

```bash
PYTHONPATH=src .venv/bin/python scripts/migrate_user_ownership.py \
  --database /absolute/path/to/test.sqlite
```

Dry-run does not create a backup, rename tables, create target tables, or write a schema version.

## Backup and Apply

Apply mode uses SQLite's backup API before starting the schema transaction. Its default backup destination is:

```text
<database>.pre-ownership-migration.bak
```

An existing backup is never overwritten. The default path is next to the selected database, so it can be inside a repository when the database itself is stored there. That is a data-exposure risk: the default backup may contain legacy Task rows, Conversation rows, and AI history. The default filename is ignored by this repository, but a production backup **must** use an explicit, controlled location outside the repository and must never be committed to Git.

Before apply, stop every application process and other writer that can access the database. Create the backup only after writes have stopped, so the backup and migration operate on the same database state. Restrict backup filesystem permissions to the migration operator, retain or destroy it according to the data-retention policy, and do not share it through source control or unapproved storage.

Apply mode then uses `BEGIN IMMEDIATE` to rename legacy tables, create the empty owner-scoped target tables, validate `integrity_check` and `foreign_key_check`, and record `ownership-v1` in `schema_migrations`.

The future apply command will be:

```bash
PYTHONPATH=src .venv/bin/python scripts/migrate_user_ownership.py \
  --database /absolute/path/to/database.sqlite \
  --apply
```

Do not run this command until every production cutover precondition below is satisfied.

## Verification

The public migration report contains only:

- mode
- database path
- legacy Task count
- legacy Conversation count
- backup path
- migration version

It never outputs passwords, password hashes, session tokens, API keys, or conversation history.

Verify the result with:

- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check`
- legacy and target row counts
- the `ownership-v1` row in `schema_migrations`
- owner-scoped application integration tests

## Rollback

Before a real rollback, stop every application process and other database writer. The original database remains unchanged when an apply transaction fails. After a successful apply, restore the verified SQLite backup as an operational rollback procedure; do not merge legacy rows into the active tables manually. After restoration, run `PRAGMA integrity_check` and `PRAGMA foreign_key_check` before allowing the application to resume writes.

## Legacy Data Policy

Legacy data remains preserved in the renamed tables and is deliberately inaccessible to the active Task and Conversation tables. It must not be assigned to the first registered user or any unverified account.

Any future ownership-claim or import workflow needs separate authentication, explicit user confirmation, and audit requirements.

## App / ORM Compatibility

The active Task and Conversation ORM models require `user_id`. Conversely, they cannot operate on the legacy schema. FastAPI startup checks existing `tasks` and `conversations` before `create_all()` and fails closed with a migration instruction when it finds an incompatible legacy table. It never alters, archives, or migrates a database automatically.

## Production Cutover Preconditions

All of the following must be complete before a real apply:

1. A verified backup, maintenance window, rollback owner, and post-cutover validation checklist exist.
2. The matching owner-scoped ORM/services/routes release is deployed together with this reviewed migration plan.
3. The frontend authentication integration is tested for the deployment origin and HTTPS Cookie settings.
