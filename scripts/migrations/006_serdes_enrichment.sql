-- Orthogonal survey dimensions used for like-for-like FoM comparisons.
-- Energy value provenance remains in serdes_measurements.energy_scope; circuit
-- coverage is stored here to avoid overloading that legacy field.

CREATE TABLE IF NOT EXISTS serdes_measurement_metric_scopes (
    measurement_id          BIGINT UNSIGNED PRIMARY KEY,
    energy_component_scope  VARCHAR(24) NOT NULL,
    scope_source            VARCHAR(24) NOT NULL,
    scope_confidence        DECIMAL(4,3) NOT NULL DEFAULT 0.000,
    reason_codes            JSON NOT NULL,
    evidence_text           TEXT,
    classifier_version      VARCHAR(32) NOT NULL,
    classified_at           DATETIME(6) NOT NULL,
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_metric_scope_measurement
        FOREIGN KEY (measurement_id) REFERENCES serdes_measurements(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_energy_component_scope
        CHECK (energy_component_scope IN
            ('tx','rx','trx','full_link','driver_only','unknown')),
    CONSTRAINT chk_serdes_metric_scope_confidence
        CHECK (scope_confidence BETWEEN 0 AND 1),
    KEY idx_serdes_metric_scope_value (energy_component_scope, scope_confidence),
    KEY idx_serdes_metric_scope_version (classifier_version, classified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_paper_link_subtypes (
    paper_id              INT PRIMARY KEY,
    link_medium           VARCHAR(16) NOT NULL,
    link_subtype          VARCHAR(32) NOT NULL,
    subtype_source        VARCHAR(24) NOT NULL,
    subtype_confidence    DECIMAL(4,3) NOT NULL DEFAULT 0.000,
    reason_codes          JSON NOT NULL,
    evidence_text         TEXT,
    classifier_version    VARCHAR(32) NOT NULL,
    classified_at         DATETIME(6) NOT NULL,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_link_subtype_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_link_subtype_medium
        CHECK (link_medium IN ('optical','electrical','unspecified')),
    CONSTRAINT chk_serdes_link_subtype_confidence
        CHECK (subtype_confidence BETWEEN 0 AND 1),
    KEY idx_serdes_link_subtype_value (link_medium, link_subtype),
    KEY idx_serdes_link_subtype_version (classifier_version, classified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
