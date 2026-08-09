PRAGMA foreign_keys = ON;

CREATE UNIQUE INDEX IF NOT EXISTS idx_file_blob_sha256_unique
  ON file_blob(sha256) WHERE sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS indicator_catalog (
  indicator_id TEXT PRIMARY KEY,
  metric_name_cn TEXT NOT NULL,
  dimension TEXT NOT NULL CHECK(dimension IN ('E', 'S', 'G')),
  metric_type TEXT NOT NULL CHECK(metric_type IN ('quantitative', 'qualitative')),
  extraction_priority TEXT NOT NULL CHECK(extraction_priority IN ('P0', 'P1', 'P2')),
  unit_normalized TEXT,
  definition TEXT,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  source_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_job (
  job_id TEXT PRIMARY KEY,
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  run_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN
    ('queued', 'running', 'succeeded', 'partial', 'failed', 'interrupted')),
  stage TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
  runner_mode TEXT NOT NULL CHECK(runner_mode IN ('live_pipeline', 'precomputed_import')),
  pipeline_version TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  log_summary TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  request_key TEXT UNIQUE,
  UNIQUE(job_id, report_version_id)
);

CREATE INDEX IF NOT EXISTS idx_job_status_created
  ON analysis_job(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_report
  ON analysis_job(report_version_id, created_at DESC);

CREATE TABLE IF NOT EXISTS extraction_result (
  result_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES analysis_job(job_id),
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  indicator_id TEXT NOT NULL REFERENCES indicator_catalog(indicator_id),
  report_year INTEGER NOT NULL CHECK(report_year BETWEEN 2000 AND 2100),
  candidate_rank INTEGER NOT NULL DEFAULT 1 CHECK(candidate_rank >= 1),
  candidate_status TEXT NOT NULL CHECK(candidate_status IN
    ('candidate_found', 'no_candidate', 'not_applicable', 'needs_review')),
  raw_value TEXT,
  normalized_value REAL,
  unit_raw TEXT,
  unit_normalized TEXT,
  confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),
  verification_status TEXT NOT NULL CHECK(verification_status IN
    ('auto_verified_high', 'auto_verified_medium', 'needs_review', 'not_verified')),
  review_status TEXT NOT NULL DEFAULT 'unreviewed' CHECK(review_status IN
    ('unreviewed', 'accepted', 'rejected', 'corrected')),
  source_kind TEXT NOT NULL CHECK(source_kind IN ('live_pipeline', 'precomputed_snapshot')),
  pipeline_version TEXT NOT NULL,
  indicator_source_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(job_id, indicator_id, candidate_rank),
  UNIQUE(result_id, report_version_id),
  FOREIGN KEY(job_id, report_version_id)
    REFERENCES analysis_job(job_id, report_version_id),
  CHECK(
    candidate_status NOT IN ('no_candidate', 'not_applicable') OR
    (raw_value IS NULL AND normalized_value IS NULL
     AND unit_raw IS NULL AND unit_normalized IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_result_report_indicator
  ON extraction_result(report_version_id, indicator_id);
CREATE INDEX IF NOT EXISTS idx_result_indicator_year
  ON extraction_result(indicator_id, report_year);
CREATE INDEX IF NOT EXISTS idx_result_job
  ON extraction_result(job_id, indicator_id, candidate_rank);

CREATE TABLE IF NOT EXISTS evidence_span (
  evidence_id TEXT PRIMARY KEY,
  result_id TEXT NOT NULL REFERENCES extraction_result(result_id),
  report_version_id TEXT NOT NULL REFERENCES report_version(report_version_id),
  page_no INTEGER CHECK(page_no IS NULL OR page_no >= 1),
  printed_page_label TEXT,
  source_text TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  bbox_json TEXT,
  source_text_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(result_id, report_version_id)
    REFERENCES extraction_result(result_id, report_version_id),
  UNIQUE(result_id, page_no, source_text_sha256, evidence_type),
  CHECK(length(trim(source_text)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_evidence_result ON evidence_span(result_id);
CREATE INDEX IF NOT EXISTS idx_evidence_report_page
  ON evidence_span(report_version_id, page_no);

CREATE TABLE IF NOT EXISTS platform_event (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_entity_time
  ON platform_event(entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_type_time
  ON platform_event(event_type, created_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_result_report_consistency_insert
BEFORE INSERT ON extraction_result
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_job j
   WHERE j.job_id=NEW.job_id
     AND j.report_version_id=NEW.report_version_id
)
BEGIN
  SELECT RAISE(ABORT, 'result_report_version_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_result_report_consistency_update
BEFORE UPDATE OF job_id, report_version_id ON extraction_result
WHEN NOT EXISTS (
  SELECT 1 FROM analysis_job j
   WHERE j.job_id=NEW.job_id
     AND j.report_version_id=NEW.report_version_id
)
BEGIN
  SELECT RAISE(ABORT, 'result_report_version_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_report_consistency_insert
BEFORE INSERT ON evidence_span
WHEN NOT EXISTS (
  SELECT 1 FROM extraction_result r
   WHERE r.result_id=NEW.result_id
     AND r.report_version_id=NEW.report_version_id
)
BEGIN
  SELECT RAISE(ABORT, 'evidence_report_version_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_report_consistency_update
BEFORE UPDATE OF result_id, report_version_id ON evidence_span
WHEN NOT EXISTS (
  SELECT 1 FROM extraction_result r
   WHERE r.result_id=NEW.result_id
     AND r.report_version_id=NEW.report_version_id
)
BEGIN
  SELECT RAISE(ABORT, 'evidence_report_version_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_job_terminal_consistency_insert
BEFORE INSERT ON analysis_job
WHEN NEW.status IN ('succeeded', 'partial', 'failed', 'interrupted')
 AND (NEW.progress <> 100 OR NEW.finished_at IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'terminal_job_requires_progress_and_finished_at');
END;

CREATE TRIGGER IF NOT EXISTS trg_job_terminal_consistency_update
BEFORE UPDATE OF status, progress, finished_at ON analysis_job
WHEN NEW.status IN ('succeeded', 'partial', 'failed', 'interrupted')
 AND (NEW.progress <> 100 OR NEW.finished_at IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'terminal_job_requires_progress_and_finished_at');
END;

PRAGMA user_version = 3;
