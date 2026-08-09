PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS import_batch (
  import_batch_id TEXT PRIMARY KEY,
  source_root TEXT NOT NULL,
  root_code TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  hash_mode TEXT NOT NULL CHECK(hash_mode IN ('computed', 'skipped')),
  status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed')),
  discovered_count INTEGER NOT NULL CHECK(discovered_count >= 0),
  accepted_count INTEGER NOT NULL DEFAULT 0 CHECK(accepted_count >= 0),
  review_count INTEGER NOT NULL DEFAULT 0 CHECK(review_count >= 0),
  idempotency_verified INTEGER NOT NULL DEFAULT 0
    CHECK(idempotency_verified IN (0, 1)),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(source_root, manifest_sha256)
);

CREATE TABLE IF NOT EXISTS company (
  company_id TEXT PRIMARY KEY,
  exchange TEXT NOT NULL CHECK(exchange = 'SSE'),
  stock_code TEXT NOT NULL
    CHECK(length(stock_code) = 6 AND stock_code NOT GLOB '*[^0-9]*'),
  legal_name TEXT,
  current_short_name TEXT,
  listing_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(listing_status IN ('listed', 'suspended', 'delisted', 'unknown')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(exchange, stock_code)
);

CREATE TABLE IF NOT EXISTS company_alias (
  alias_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES company(company_id),
  alias_name TEXT NOT NULL,
  alias_type TEXT NOT NULL DEFAULT 'short_name',
  valid_from TEXT,
  valid_to TEXT,
  source TEXT NOT NULL,
  UNIQUE(company_id, alias_name, valid_from)
);

CREATE TABLE IF NOT EXISTS report (
  report_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES company(company_id),
  report_year INTEGER NOT NULL CHECK(report_year IN (2023, 2024, 2025)),
  report_type_key TEXT NOT NULL,
  primary_report_type TEXT NOT NULL
    CHECK(primary_report_type IN ('ESG', 'SD', 'CSR', 'ENV', 'OTHER')),
  language_code TEXT NOT NULL DEFAULT 'zh-CN',
  scope_code TEXT NOT NULL DEFAULT 'unknown',
  edition_no INTEGER NOT NULL DEFAULT 1 CHECK(edition_no >= 1),
  source_site TEXT,
  source_announcement_id TEXT,
  canonical_title TEXT NOT NULL,
  logical_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active', 'superseded', 'withdrawn', 'review')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(company_id, report_year, report_type_key,
         language_code, scope_code, edition_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_report_announcement
  ON report(source_site, source_announcement_id)
  WHERE source_announcement_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS report_type_tag (
  report_id TEXT NOT NULL REFERENCES report(report_id),
  type_code TEXT NOT NULL
    CHECK(type_code IN ('ESG', 'SD', 'CSR', 'ENV', 'OTHER')),
  PRIMARY KEY(report_id, type_code)
);

-- file_blob_id remains usable in quick-audit mode when sha256 is intentionally NULL.
-- With hashing enabled it is derived from SHA-256, so identical bytes share one blob.
CREATE TABLE IF NOT EXISTS file_blob (
  file_blob_id TEXT PRIMARY KEY,
  sha256 TEXT CHECK(
    sha256 IS NULL OR
    (length(sha256) = 64 AND lower(sha256) = sha256
     AND sha256 NOT GLOB '*[^0-9a-f]*')
  ),
  file_size_bytes INTEGER NOT NULL CHECK(file_size_bytes > 0),
  mime_type TEXT NOT NULL DEFAULT 'application/pdf',
  pdf_header_ok INTEGER NOT NULL CHECK(pdf_header_ok IN (0, 1)),
  pdf_eof_ok INTEGER NOT NULL CHECK(pdf_eof_ok IN (0, 1)),
  hash_state TEXT NOT NULL CHECK(hash_state IN ('computed', 'skipped')),
  modified_time_ns INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_blob_sha256
  ON file_blob(sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS report_version (
  report_version_id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES report(report_id),
  file_blob_id TEXT NOT NULL REFERENCES file_blob(file_blob_id),
  version_no INTEGER NOT NULL CHECK(version_no >= 1),
  content_key TEXT NOT NULL,
  disclosure_date TEXT,
  source_url TEXT,
  original_file_name TEXT NOT NULL,
  short_name_raw TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0, 1)),
  version_reason TEXT NOT NULL
    CHECK(version_reason IN
      ('initial', 'revised', 'redownloaded', 'metadata_fix', 'unknown')),
  verification_status TEXT NOT NULL
    CHECK(verification_status IN ('accepted', 'review', 'quarantined', 'rejected')),
  quality_flags_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(report_id, version_no),
  UNIQUE(report_id, content_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_report_current_version
  ON report_version(report_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS file_location (
  location_id TEXT PRIMARY KEY,
  file_blob_id TEXT NOT NULL REFERENCES file_blob(file_blob_id),
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  root_code TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  is_available INTEGER NOT NULL DEFAULT 1 CHECK(is_available IN (0, 1)),
  UNIQUE(root_code, relative_path)
);

CREATE TABLE IF NOT EXISTS coverage_slot (
  company_id TEXT NOT NULL REFERENCES company(company_id),
  report_year INTEGER NOT NULL CHECK(report_year IN (2023, 2024, 2025)),
  expected_in_scope INTEGER NOT NULL DEFAULT 1
    CHECK(expected_in_scope IN (0, 1)),
  coverage_status TEXT NOT NULL CHECK(coverage_status IN
    ('present', 'missing_not_found', 'pending_verification',
     'not_expected', 'excluded_scope')),
  canonical_report_id TEXT REFERENCES report(report_id),
  canonical_report_version_id TEXT REFERENCES report_version(report_version_id),
  reason_code TEXT,
  checked_at TEXT NOT NULL,
  PRIMARY KEY(company_id, report_year)
);

CREATE TABLE IF NOT EXISTS dataset_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  snapshot_code TEXT NOT NULL UNIQUE,
  snapshot_label TEXT NOT NULL CHECK(snapshot_label IN ('P200', 'P177', 'P531')),
  member_kind TEXT NOT NULL CHECK(member_kind IN ('company', 'report', 'mixed')),
  definition TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  frozen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_company_member (
  snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
  company_id TEXT NOT NULL REFERENCES company(company_id),
  member_rank INTEGER,
  PRIMARY KEY(snapshot_id, company_id)
);

CREATE TABLE IF NOT EXISTS dataset_member (
  snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  member_rank INTEGER,
  PRIMARY KEY(snapshot_id, report_version_id)
);

CREATE TABLE IF NOT EXISTS import_manifest_row (
  import_batch_id TEXT NOT NULL REFERENCES import_batch(import_batch_id),
  manifest_row_id TEXT NOT NULL,
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  row_sha256 TEXT NOT NULL CHECK(length(row_sha256) = 64),
  row_json TEXT NOT NULL,
  PRIMARY KEY(import_batch_id, manifest_row_id)
);

CREATE INDEX IF NOT EXISTS ix_report_company_year
  ON report(company_id, report_year);
CREATE INDEX IF NOT EXISTS ix_version_blob
  ON report_version(file_blob_id);
CREATE INDEX IF NOT EXISTS ix_location_version
  ON file_location(report_version_id);
CREATE INDEX IF NOT EXISTS ix_coverage_year_status
  ON coverage_slot(report_year, coverage_status);
CREATE INDEX IF NOT EXISTS ix_snapshot_label
  ON dataset_snapshot(snapshot_label, manifest_sha256);
