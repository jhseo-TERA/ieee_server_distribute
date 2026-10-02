-- Favorite-derived profiles are separate from papers and measured results.
CREATE TABLE IF NOT EXISTS ai_recommendation_profiles (
    profile_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    model VARCHAR(191) NOT NULL,
    version VARCHAR(32) NOT NULL,
    payload_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
