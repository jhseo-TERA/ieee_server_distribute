-- Canonical one-current-abstract view across permitted metadata providers.
-- Official IEEE metadata wins whenever it exists; open scholarly indexes are
-- used only as a fallback. The underlying immutable revisions remain intact.

CREATE OR REPLACE VIEW paper_current_abstracts AS
SELECT candidate.*
FROM paper_abstracts candidate
WHERE candidate.is_current = 1
  AND NOT EXISTS (
      SELECT 1
      FROM paper_abstracts better
      WHERE better.paper_id = candidate.paper_id
        AND better.is_current = 1
        AND (
            CASE better.provider
                WHEN 'manual' THEN 100
                WHEN 'ieee_metadata' THEN 80
                WHEN 'openalex' THEN 60
                WHEN 'crossref' THEN 40
                ELSE 20
            END
            >
            CASE candidate.provider
                WHEN 'manual' THEN 100
                WHEN 'ieee_metadata' THEN 80
                WHEN 'openalex' THEN 60
                WHEN 'crossref' THEN 40
                ELSE 20
            END
            OR (
                CASE better.provider
                    WHEN 'manual' THEN 100
                    WHEN 'ieee_metadata' THEN 80
                    WHEN 'openalex' THEN 60
                    WHEN 'crossref' THEN 40
                    ELSE 20
                END
                =
                CASE candidate.provider
                    WHEN 'manual' THEN 100
                    WHEN 'ieee_metadata' THEN 80
                    WHEN 'openalex' THEN 60
                    WHEN 'crossref' THEN 40
                    ELSE 20
                END
                AND better.id > candidate.id
            )
        )
  )
