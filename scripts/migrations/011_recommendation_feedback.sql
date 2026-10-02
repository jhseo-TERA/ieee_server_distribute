-- Explicit shared exclusions; restore deletes only the feedback row.
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    article_number VARCHAR(255) NOT NULL PRIMARY KEY,
    action VARCHAR(20) NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
