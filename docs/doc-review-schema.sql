-- Install separately from PR workers. No changes to translator tables.
CREATE TABLE IF NOT EXISTS doc_review_approvals (
 repository Utf8 NOT NULL, pr Uint64 NOT NULL, head Utf8 NOT NULL,
 actor Utf8 NOT NULL, event_id Utf8 NOT NULL, approved_at Timestamp,
 PRIMARY KEY (repository, pr, head)
);
CREATE TABLE IF NOT EXISTS doc_review_claims (
 repository Utf8 NOT NULL, pr Uint64 NOT NULL, request_id Utf8 NOT NULL, owner Utf8, phase Utf8,
 PRIMARY KEY (repository, pr, request_id)
);
CREATE TABLE IF NOT EXISTS doc_review_leases (
 repository Utf8 NOT NULL, pr Uint64 NOT NULL, head Utf8, owner Utf8,
 PRIMARY KEY (repository, pr)
);
CREATE TABLE IF NOT EXISTS doc_review_runs (
 owner Utf8 NOT NULL, repository Utf8, pr Uint64, head Utf8, status Utf8,
 updated_at Timestamp, report String,
 PRIMARY KEY (owner)
);
CREATE TABLE IF NOT EXISTS doc_review_attempts (
 owner Utf8 NOT NULL, attempt Uint64 NOT NULL, kind Utf8,
 reservation Decimal(22,9), cost Decimal(22,9), updated_at Timestamp, usage String,
 PRIMARY KEY (owner, attempt)
);
