-- Survey citations are independent of the Repo favorite-only citation policy.
CREATE TABLE IF NOT EXISTS serdes_paper_bibliography (
    paper_id INT PRIMARY KEY,
    doi VARCHAR(255) NOT NULL,
    authors TEXT NULL,
    citation_count INT UNSIGNED NULL,
    provider VARCHAR(24) NOT NULL,
    identity_method VARCHAR(40) NOT NULL,
    source_url VARCHAR(600) NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    record_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_serdes_bibliography_paper FOREIGN KEY (paper_id) REFERENCES papers(id),
    KEY idx_serdes_bibliography_citations (citation_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
