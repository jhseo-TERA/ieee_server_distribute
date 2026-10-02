-- Auditable physical-medium classification kept separate from link topology.
-- A paper may be die-to-die and optical at the same time, so this must not
-- reuse serdes_measurements.link_class.

CREATE TABLE IF NOT EXISTS serdes_paper_link_media (
    paper_id             INT PRIMARY KEY,
    abstract_id          BIGINT UNSIGNED NULL,
    link_medium          VARCHAR(16) NOT NULL,
    medium_source        VARCHAR(24) NOT NULL,
    medium_confidence    DECIMAL(4,3) NOT NULL DEFAULT 0.000,
    reason_codes         JSON NOT NULL,
    evidence_text        TEXT,
    classifier_version   VARCHAR(32) NOT NULL,
    classified_at        DATETIME(6) NOT NULL,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_medium_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_medium_abstract
        FOREIGN KEY (abstract_id) REFERENCES paper_abstracts(id) ON DELETE SET NULL,
    CONSTRAINT chk_serdes_medium_value
        CHECK (link_medium IN ('optical', 'electrical', 'unspecified')),
    CONSTRAINT chk_serdes_medium_confidence
        CHECK (medium_confidence BETWEEN 0 AND 1),
    KEY idx_serdes_medium_value (link_medium, medium_confidence),
    KEY idx_serdes_medium_version (classifier_version, classified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
