CREATE TABLE IF NOT EXISTS account_login_sessions (
    session_key VARCHAR(64) NOT NULL PRIMARY KEY,
    account_id BIGINT UNSIGNED NOT NULL,
    session_version INT NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    login_at DATETIME(6) NULL,
    last_seen_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    end_reason VARCHAR(16) NULL,
    KEY idx_account_login_seen (account_id,last_seen_at),
    KEY idx_account_login_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
