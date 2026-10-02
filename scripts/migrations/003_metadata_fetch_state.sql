-- Auditable, resumable IEEE abstract acquisition state.
-- Terminal misses are retained so a full-venue fetch can prove that every
-- paper was attempted without repeatedly starving later rows.

CREATE TABLE IF NOT EXISTS paper_metadata_fetch_state (
    paper_id            INT NOT NULL,
    provider            VARCHAR(32) NOT NULL,
    fetch_status        VARCHAR(24) NOT NULL,
    attempt_count       INT UNSIGNED NOT NULL DEFAULT 1,
    detail              VARCHAR(500) NULL,
    last_attempt_at     DATETIME(6) NOT NULL,
    completed_at        DATETIME(6) NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (paper_id, provider),
    CONSTRAINT fk_paper_metadata_fetch_state_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    KEY idx_metadata_fetch_status (provider, fetch_status, last_attempt_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
