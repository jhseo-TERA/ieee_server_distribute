-- Reproducible paper-level screening for venue audits such as PTL.
-- This remains additive: the source papers and abstract snapshots are unchanged.

CREATE TABLE IF NOT EXISTS serdes_screening_runs (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    venue               VARCHAR(100) NOT NULL,
    scope_name          VARCHAR(24) NOT NULL,
    scope_version       VARCHAR(32) NOT NULL,
    target_count        INT UNSIGNED NOT NULL DEFAULT 0,
    core_count          INT UNSIGNED NOT NULL DEFAULT 0,
    adjacent_count      INT UNSIGNED NOT NULL DEFAULT 0,
    review_count        INT UNSIGNED NOT NULL DEFAULT 0,
    excluded_count      INT UNSIGNED NOT NULL DEFAULT 0,
    abstract_count      INT UNSIGNED NOT NULL DEFAULT 0,
    status              VARCHAR(20) NOT NULL DEFAULT 'running',
    started_at          DATETIME(6) NOT NULL,
    completed_at        DATETIME(6) NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    KEY idx_serdes_screening_run (venue, scope_version, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_paper_screenings (
    paper_id            INT PRIMARY KEY,
    run_id              BIGINT UNSIGNED NOT NULL,
    abstract_id         BIGINT UNSIGNED NULL,
    venue               VARCHAR(100) NOT NULL,
    scope_version       VARCHAR(32) NOT NULL,
    relevance_class     VARCHAR(24) NOT NULL,
    relevance_score     DECIMAL(6,3) NOT NULL,
    include_in_survey   BOOLEAN NOT NULL DEFAULT 0,
    reason_codes        JSON NOT NULL,
    rationale           VARCHAR(1000) NOT NULL,
    screening_source    VARCHAR(24) NOT NULL DEFAULT 'rules',
    review_status       VARCHAR(24) NOT NULL DEFAULT 'auto_screened',
    screened_at         DATETIME(6) NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_screening_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_screening_run
        FOREIGN KEY (run_id) REFERENCES serdes_screening_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_screening_abstract
        FOREIGN KEY (abstract_id) REFERENCES paper_abstracts(id) ON DELETE SET NULL,
    CONSTRAINT chk_serdes_screening_score
        CHECK (relevance_score BETWEEN 0 AND 100),
    KEY idx_serdes_screening_venue (venue, relevance_score, relevance_class),
    KEY idx_serdes_screening_class (relevance_class, include_in_survey)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
