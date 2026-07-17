# Five-Minute Enrichment Schedule Design

## Goal

Run the company enrichment workflow every five minutes, process at most 500
companies per phase in each run, and publish changed SQLite data to the existing
rolling GitHub Release asset.

If another run of the same workflow is already queued or in progress, the new
scheduled run must finish successfully without doing enrichment work. A later
cron tick will try again after five minutes.

## Current State

`.github/workflows/enrich.yml` already:

- defaults to a 500-company limit;
- restores and publishes `companies.db.gz` through the `db-latest` release;
- supports manual runs;
- defines a five-minute cron, but the schedule is commented out; and
- uses GitHub Actions concurrency, which queues overlapping runs rather than
  skipping them.

## Considered Approaches

1. **Preflight guard job (selected).** Query the GitHub Actions API for other
   queued or in-progress runs of this workflow. Emit an output that gates the
   enrichment job. This provides the requested successful skip behavior while
   leaving future cron ticks enabled.
2. **GitHub Actions concurrency.** This safely serializes database updates, but
   pending runs queue and execute later, contrary to the no-queue requirement.
3. **Cancel in-progress runs.** Setting `cancel-in-progress: true` avoids a
   queue, but interrupts database processing and contradicts the requirement to
   let the current run finish.

## Workflow Design

- Enable `schedule` with `cron: "*/5 * * * *"`.
- Keep `workflow_dispatch` and its existing `limit` and `workers` inputs.
- Add `actions: read` permission so the workflow can inspect its runs.
- Remove the workflow-level concurrency group.
- Add a lightweight `guard` job before enrichment:
  - inspect runs for `.github/workflows/enrich.yml`;
  - ignore the current run ID;
  - treat both `queued` and `in_progress` runs as active;
  - expose `should_run=true` only when no other active run exists; and
  - write a clear job summary when enrichment is skipped.
- Make the existing `enrich` job depend on `guard` and run only when
  `should_run` is true.
- Preserve the existing 500 default, 16-worker default, timeout, incremental
  batch command, change detection, and release upload behavior.

Gating the entire enrichment job is required: merely exiting the guard step
successfully would allow subsequent steps in that same job to continue.

## Data and Failure Behavior

The selected guard prevents normal overlap and avoids a backlog. GitHub's API
check is not an atomic lock, so two runs started almost simultaneously could
both observe no other active run. GitHub cron normally creates one run per tick,
making this race unlikely. A strict overlap guarantee would require queueing,
an external lock, or cancellation, none of which matches the requested
behavior.

If the guard API request fails, the guard job fails closed: enrichment does not
run, avoiding concurrent writes. The next five-minute cron tick retries.

The enrichment pipeline remains incremental and resumable. The database is
uploaded only when the batch reports a change.

## Verification

- Validate the workflow YAML syntax.
- Run `actionlint` if it is available.
- Confirm the cron, permissions, guard output, job dependency, 500 default, and
  removal of the concurrency queue in the final diff.
- Manual end-to-end validation can trigger one run while another is active and
  confirm that the newer run skips its enrichment job.
