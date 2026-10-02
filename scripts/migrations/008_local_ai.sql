-- Local AI work is staged separately from authoritative survey records.
-- This migration is additive and contains no data replacement or removal.
CREATE TABLE IF NOT EXISTS ai_jobs (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner VARCHAR(191) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    model VARCHAR(191) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    request_json JSON NOT NULL,
    result_json JSON NULL,
    error TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_ai_job_owner (owner, created_at),
    KEY idx_ai_job_status (status, created_at),
    CONSTRAINT chk_ai_job_status CHECK (status IN ('queued','running','complete','failed','cancelled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_proposals (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    paper_id INT NOT NULL,
    field_name VARCHAR(48) NOT NULL,
    proposed_value JSON NOT NULL,
    unit VARCHAR(24) NOT NULL,
    source_page INT UNSIGNED NULL,
    evidence_quote TEXT NULL,
    source_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    operating_point VARCHAR(160) NOT NULL,
    validation_status VARCHAR(24) NOT NULL,
    payload_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    reviewer VARCHAR(191) NULL,
    reviewed_at DATETIME(6) NULL,
    measurement_id BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY idx_ai_proposal_pending (status, created_at),
    KEY idx_ai_proposal_job (job_id, paper_id),
    CONSTRAINT fk_ai_proposal_job FOREIGN KEY (job_id) REFERENCES ai_jobs(id),
    CONSTRAINT fk_ai_proposal_paper FOREIGN KEY (paper_id) REFERENCES papers(id),
    CONSTRAINT fk_ai_proposal_measurement FOREIGN KEY (measurement_id) REFERENCES serdes_measurements(id),
    CONSTRAINT chk_ai_proposal_status CHECK (status IN ('pending','approved','rejected'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
