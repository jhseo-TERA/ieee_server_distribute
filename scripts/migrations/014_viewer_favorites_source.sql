-- Apply before deploying the favorite_sql reader. NULL preserves private lists.
ALTER TABLE accounts
    ADD COLUMN favorites_source_account_id BIGINT UNSIGNED NULL,
    ADD CONSTRAINT fk_account_favorites_source
        FOREIGN KEY (favorites_source_account_id) REFERENCES accounts(id),
    ADD CONSTRAINT chk_viewer_favorites_source
        CHECK (favorites_source_account_id IS NULL OR role='viewer');
