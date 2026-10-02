-- Non-destructive conference/journal implementation families and human overrides.
-- Existing implementations, measurements, and evidence are intentionally unchanged.

CREATE TABLE IF NOT EXISTS serdes_measurement_scope_overrides (
    measurement_id          BIGINT UNSIGNED PRIMARY KEY,
    energy_component_scope  VARCHAR(24) NOT NULL,
    override_source         VARCHAR(24) NOT NULL DEFAULT 'manual',
    reviewer                VARCHAR(120),
    reason                  TEXT,
    evidence_text           TEXT,
    review_status           VARCHAR(20) NOT NULL DEFAULT 'verified',
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_scope_override_measurement
        FOREIGN KEY (measurement_id) REFERENCES serdes_measurements(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_scope_override_value
        CHECK (energy_component_scope IN
            ('tx','rx','trx','full_link','driver_only','unknown')),
    KEY idx_serdes_scope_override_status (review_status, energy_component_scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_paper_subtype_overrides (
    paper_id                INT PRIMARY KEY,
    link_medium            VARCHAR(16) NOT NULL,
    link_subtype           VARCHAR(32) NOT NULL,
    subtype_flags          JSON,
    override_source        VARCHAR(24) NOT NULL DEFAULT 'manual',
    reviewer               VARCHAR(120),
    reason                 TEXT,
    evidence_text          TEXT,
    review_status          VARCHAR(20) NOT NULL DEFAULT 'verified',
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_subtype_override_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_subtype_override_medium
        CHECK (link_medium IN ('optical','electrical','unspecified')),
    CONSTRAINT chk_serdes_subtype_override_value
        CHECK (link_subtype IN
            ('vcsel','silicon_photonic','eml_dml','pon','optical_other',
             'die_to_die','memory_io','backplane','cable','chip_to_chip',
             'electrical_other','mixed_or_unknown')),
    KEY idx_serdes_subtype_override_status (review_status, link_medium, link_subtype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_implementation_families (
    id                     BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    family_key             VARCHAR(191) NOT NULL,
    canonical_paper_id     INT NOT NULL,
    family_method          VARCHAR(40) NOT NULL,
    family_confidence      DECIMAL(4,3) NOT NULL,
    classifier_version     VARCHAR(48) NOT NULL,
    review_status          VARCHAR(20) NOT NULL DEFAULT 'active',
    notes                  TEXT,
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_family_canonical_paper
        FOREIGN KEY (canonical_paper_id) REFERENCES papers(id) ON DELETE RESTRICT,
    CONSTRAINT chk_serdes_family_confidence
        CHECK (family_confidence BETWEEN 0 AND 1),
    UNIQUE KEY uq_serdes_family_key (family_key),
    UNIQUE KEY uq_serdes_family_canonical_paper (canonical_paper_id),
    KEY idx_serdes_family_status (review_status, classifier_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_implementation_match_candidates (
    id                         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    candidate_key              CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    conference_paper_id        INT NOT NULL,
    journal_paper_id           INT NOT NULL,
    conference_implementation_id BIGINT UNSIGNED NULL,
    journal_implementation_id  BIGINT UNSIGNED NULL,
    family_id                  BIGINT UNSIGNED NULL,
    candidate_tier             VARCHAR(24) NOT NULL,
    decision_status            VARCHAR(24) NOT NULL,
    decision_source            VARCHAR(24) NOT NULL DEFAULT 'classifier',
    match_score                DECIMAL(6,5) NOT NULL,
    title_similarity           DECIMAL(6,5) NOT NULL,
    title_token_jaccard        DECIMAL(6,5) NOT NULL,
    shared_author_count        SMALLINT UNSIGNED NOT NULL,
    author_jaccard             DECIMAL(6,5) NOT NULL,
    first_author_match         BOOLEAN NOT NULL DEFAULT 0,
    year_gap                   TINYINT UNSIGNED NOT NULL,
    technical_matches          JSON NOT NULL,
    technical_conflicts        JSON NOT NULL,
    reason_codes               JSON NOT NULL,
    classifier_version         VARCHAR(48) NOT NULL,
    decision_note              TEXT,
    decided_by                 VARCHAR(120),
    decided_at                 DATETIME(6),
    first_seen_at              DATETIME(6) NOT NULL,
    last_seen_at               DATETIME(6) NOT NULL,
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_match_conference_paper
        FOREIGN KEY (conference_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_match_journal_paper
        FOREIGN KEY (journal_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_match_conference_impl
        FOREIGN KEY (conference_implementation_id) REFERENCES serdes_implementations(id) ON DELETE SET NULL,
    CONSTRAINT fk_serdes_match_journal_impl
        FOREIGN KEY (journal_implementation_id) REFERENCES serdes_implementations(id) ON DELETE SET NULL,
    CONSTRAINT fk_serdes_match_family
        FOREIGN KEY (family_id) REFERENCES serdes_implementation_families(id) ON DELETE SET NULL,
    CONSTRAINT chk_serdes_match_distinct_papers
        CHECK (conference_paper_id <> journal_paper_id),
    CONSTRAINT chk_serdes_match_tier
        CHECK (candidate_tier IN ('strict','manual_review')),
    CONSTRAINT chk_serdes_match_decision
        CHECK (decision_status IN
            ('auto_grouped','manual_review','approved','rejected')),
    CONSTRAINT chk_serdes_match_score
        CHECK (match_score BETWEEN 0 AND 1),
    UNIQUE KEY uq_serdes_match_candidate_key (candidate_key),
    UNIQUE KEY uq_serdes_match_pair (conference_paper_id, journal_paper_id),
    KEY idx_serdes_match_decision (decision_status, candidate_tier, match_score),
    KEY idx_serdes_match_family (family_id),
    KEY idx_serdes_match_journal (journal_paper_id, conference_paper_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Append-only provenance for explicit reviewer actions and classifier
-- transitions.  The candidate row remains the current state used by queries.
CREATE TABLE IF NOT EXISTS serdes_implementation_match_decision_events (
    id                         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    candidate_id               BIGINT UNSIGNED NOT NULL,
    previous_decision_status   VARCHAR(24),
    decision_status            VARCHAR(24) NOT NULL,
    decision_source            VARCHAR(24) NOT NULL,
    decision_note              TEXT,
    decided_by                 VARCHAR(120),
    decided_at                 DATETIME(6) NOT NULL,
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_serdes_match_decision_event_candidate
        FOREIGN KEY (candidate_id)
        REFERENCES serdes_implementation_match_candidates(id) ON DELETE CASCADE,
    CONSTRAINT chk_serdes_match_decision_event_status
        CHECK (decision_status IN
            ('auto_grouped','manual_review','approved','rejected')),
    KEY idx_serdes_match_decision_event_candidate
        (candidate_id, decided_at, id),
    KEY idx_serdes_match_decision_event_status
        (decision_status, decision_source, decided_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS serdes_implementation_family_members (
    family_id             BIGINT UNSIGNED NOT NULL,
    paper_id              INT NOT NULL,
    implementation_id     BIGINT UNSIGNED NULL,
    match_candidate_id    BIGINT UNSIGNED NULL,
    relation_type         VARCHAR(32) NOT NULL,
    is_canonical          BOOLEAN NOT NULL DEFAULT 0,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (family_id, paper_id),
    CONSTRAINT fk_serdes_family_member_family
        FOREIGN KEY (family_id) REFERENCES serdes_implementation_families(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_family_member_paper
        FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    CONSTRAINT fk_serdes_family_member_impl
        FOREIGN KEY (implementation_id) REFERENCES serdes_implementations(id) ON DELETE SET NULL,
    CONSTRAINT fk_serdes_family_member_candidate
        FOREIGN KEY (match_candidate_id) REFERENCES serdes_implementation_match_candidates(id) ON DELETE SET NULL,
    UNIQUE KEY uq_serdes_family_member_paper (paper_id),
    KEY idx_serdes_family_member_impl (implementation_id, family_id),
    KEY idx_serdes_family_member_canonical (family_id, is_canonical)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
