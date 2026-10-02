-- Derived, validated technical pair scores only; no changes to papers/favorites.
CREATE TABLE IF NOT EXISTS ai_recommendation_pair_cache (
    cache_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    payload_json JSON NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY idx_recommendation_pair_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
