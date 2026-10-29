-- Daily selections and notes are private to authenticated server-owned identities.
CREATE TABLE IF NOT EXISTS daily_review_batches (
    owner VARCHAR(80) NOT NULL,
    review_date DATE NOT NULL,
    PRIMARY KEY (owner,review_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS daily_review_picks (
    owner VARCHAR(80) NOT NULL,
    review_date DATE NOT NULL,
    slot INT NOT NULL,
    article_number VARCHAR(80) NOT NULL,
    PRIMARY KEY (owner,review_date,slot),
    FOREIGN KEY (owner,review_date) REFERENCES daily_review_batches(owner,review_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS personal_paper_reviews (
    owner VARCHAR(80) NOT NULL,
    article_number VARCHAR(80) NOT NULL,
    notes TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    version INT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (owner,article_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
