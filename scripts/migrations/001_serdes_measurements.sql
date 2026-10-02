-- Additive SerDes survey schema. Safe to run repeatedly on an existing database.
-- The core papers table is intentionally unchanged.

CREATE TABLE IF NOT EXISTS paper_abstracts (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    paper_id         INT NOT NULL,
    provider         VARCHAR(32) NOT NULL,
    provider_record_id VARCHAR(120),
    source_url       VARCHAR(800) NOT NULL,
    abstract_text    MEDIUMTEXT NOT NULL,
    content_sha256   CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    retrieved_at     DATETIME(6) NOT NULL,
    is_current       BOOLEAN NOT NULL DEFAULT 1,
    current_slot     TINYINT GENERATED ALWAYS AS (IF(is_current, 1, NULL)) STORED,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_paper_abstracts_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    UNIQUE KEY uq_paper_abstract_revision (paper_id, provider, content_sha256),
    UNIQUE KEY uq_paper_abstract_current (paper_id, provider, current_slot),
    KEY idx_paper_abstract_lookup (paper_id, is_current, retrieved_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_implementations (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    implementation_key  VARCHAR(191) NOT NULL,
    canonical_paper_id  INT NULL,
    canonical_title     TEXT,
    canonical_year      SMALLINT UNSIGNED,
    canonical_venue     VARCHAR(100),
    dedup_method        VARCHAR(32) NOT NULL DEFAULT 'single_paper',
    dedup_confidence    DECIMAL(4,3) NOT NULL DEFAULT 1.000,
    review_status       VARCHAR(20) NOT NULL DEFAULT 'extracted',
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_impl_canonical_paper
        FOREIGN KEY (canonical_paper_id) REFERENCES papers(id) ON DELETE SET NULL,
    CONSTRAINT chk_serdes_impl_confidence
        CHECK (dedup_confidence BETWEEN 0 AND 1),
    UNIQUE KEY uq_serdes_implementation_key (implementation_key),
    KEY idx_serdes_impl_paper (canonical_paper_id),
    KEY idx_serdes_impl_year (canonical_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_implementation_papers (
    implementation_id BIGINT UNSIGNED NOT NULL,
    paper_id           INT NOT NULL,
    relation_type      VARCHAR(32) NOT NULL DEFAULT 'primary',
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (implementation_id, paper_id),
    KEY idx_serdes_impl_papers_paper (paper_id, implementation_id),
    CONSTRAINT fk_serdes_impl_papers_impl
        FOREIGN KEY (implementation_id) REFERENCES serdes_implementations(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_impl_papers_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_measurements (
    id                       BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    implementation_id        BIGINT UNSIGNED NOT NULL,
    operating_point_key      VARCHAR(64) NOT NULL DEFAULT 'primary',
    reference_title          TEXT,
    publication_year         SMALLINT UNSIGNED,
    publication_name         VARCHAR(100),
    first_author             VARCHAR(255),
    affiliation              VARCHAR(500),

    link_class               VARCHAR(32),
    component_scope          VARCHAR(24) NOT NULL DEFAULT 'unknown',
    modulation               VARCHAR(24),
    process_text             VARCHAR(80),
    process_nm               DECIMAL(8,3),

    reported_rate_text       VARCHAR(160),
    reported_rate_gbps       DECIMAL(14,6),
    reported_rate_min_gbps   DECIMAL(14,6),
    reported_rate_max_gbps   DECIMAL(14,6),
    rate_scope               VARCHAR(20) NOT NULL DEFAULT 'unknown',
    lane_rate_gbps           DECIMAL(14,6),
    lane_count               SMALLINT UNSIGNED,
    aggregate_rate_gbps      DECIMAL(14,6),
    aggregate_rate_basis     VARCHAR(24),
    symbol_rate_gbaud        DECIMAL(14,6),
    throughput_density_gbps_per_mm DECIMAL(14,6),

    power_mw                 DECIMAL(16,6),
    power_scope              VARCHAR(24) NOT NULL DEFAULT 'unknown',
    energy_pj_bit            DECIMAL(16,9),
    energy_scope             VARCHAR(24) NOT NULL DEFAULT 'unknown',
    energy_basis             VARCHAR(32),
    energy_loss_normalized_pj_bit_db DECIMAL(16,9),

    channel_loss_db          DECIMAL(10,4),
    loss_frequency_ghz       DECIMAL(12,6),
    ber                      DOUBLE,
    ber_scope                VARCHAR(24) NOT NULL DEFAULT 'unknown',
    active_area_mm2          DECIMAL(14,6),

    source_kind              VARCHAR(24) NOT NULL,
    overall_confidence       DECIMAL(4,3) NOT NULL DEFAULT 0.700,
    review_status            VARCHAR(20) NOT NULL DEFAULT 'extracted',
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_measurement_impl
        FOREIGN KEY (implementation_id) REFERENCES serdes_implementations(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_measurement_confidence
        CHECK (overall_confidence BETWEEN 0 AND 1),
    CONSTRAINT chk_serdes_measurement_lane_count
        CHECK (lane_count IS NULL OR lane_count > 0),
    CONSTRAINT chk_serdes_measurement_values
        CHECK (
            (process_nm IS NULL OR process_nm > 0) AND
            (reported_rate_gbps IS NULL OR reported_rate_gbps > 0) AND
            (lane_rate_gbps IS NULL OR lane_rate_gbps > 0) AND
            (aggregate_rate_gbps IS NULL OR aggregate_rate_gbps > 0) AND
            (energy_pj_bit IS NULL OR energy_pj_bit > 0) AND
            (power_mw IS NULL OR power_mw > 0)
        ),
    UNIQUE KEY uq_serdes_measurement_point (implementation_id, operating_point_key),
    KEY idx_serdes_measurement_year (publication_year),
    KEY idx_serdes_measurement_energy (energy_pj_bit),
    KEY idx_serdes_measurement_rate (lane_rate_gbps),
    KEY idx_serdes_measurement_source (source_kind, review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_measurement_evidence (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    evidence_key        CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    measurement_id      BIGINT UNSIGNED NOT NULL,
    field_name          VARCHAR(48) NOT NULL,
    paper_id            INT NULL,
    abstract_id         BIGINT UNSIGNED NULL,
    source_kind         VARCHAR(24) NOT NULL,
    source_url          VARCHAR(800),
    source_locator      VARCHAR(120),
    evidence_text       TEXT NOT NULL,
    source_sha256       CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    evidence_sha256     CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    extraction_method   VARCHAR(64) NOT NULL,
    extractor_version   VARCHAR(32) NOT NULL,
    confidence          DECIMAL(4,3) NOT NULL DEFAULT 0.700,
    review_status       VARCHAR(20) NOT NULL DEFAULT 'extracted',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_evidence_measurement
        FOREIGN KEY (measurement_id) REFERENCES serdes_measurements(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_evidence_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_evidence_abstract
        FOREIGN KEY (abstract_id) REFERENCES paper_abstracts(id) ON DELETE SET NULL,
    CONSTRAINT chk_serdes_evidence_confidence
        CHECK (confidence BETWEEN 0 AND 1),
    UNIQUE KEY uq_serdes_evidence_key (evidence_key),
    KEY idx_serdes_evidence_measurement (measurement_id, field_name),
    KEY idx_serdes_evidence_paper (paper_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
