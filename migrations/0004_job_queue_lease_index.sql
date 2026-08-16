-- =============================================================================
-- 0004_job_queue_lease_index.sql — support for job-lease-expiry reclaim
--
-- `idx_jobs_claimable` (0001_init.sql) covers "find pending work that's due" --
-- the query a worker runs to claim its next batch. This adds the matching
-- index for the complementary maintenance query: "find processing work whose
-- lease has expired" (a worker died or was killed mid-job, and the row would
-- otherwise stay locked forever). See arie.jobs.queue.PostgresJobQueue.claim,
-- which reclaims expired leases before claiming new work, in the same
-- transaction.
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_jobs_lease_expiry
    ON jobs(locked_at) WHERE status = 'processing';
