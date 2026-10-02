-- IEEE Paper Repository - MySQL schema
-- 실행: mysql -u root -p < scripts/schema.sql

CREATE DATABASE IF NOT EXISTS ieee_repo
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE ieee_repo;

CREATE TABLE IF NOT EXISTS papers (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    article_number  VARCHAR(80) UNIQUE,          -- IEEE: document id(숫자) / Optica·Nature: uri/DOI 키
    title           TEXT NOT NULL,
    authors         TEXT,
    year            VARCHAR(10),
    source_name     VARCHAR(50),                 -- JSSC / ISSCC / PR / OE / NPHOTON / NCOMMS ...
    source_type     VARCHAR(10),                 -- journal / conference
    source_system   VARCHAR(10) NOT NULL DEFAULT 'optica',  -- ieee / optica / nature / designcon
    issue           VARCHAR(120),                -- 저널: Issue / 학회: Page / Optica·Nature: Category
    url             VARCHAR(400),
    pdf_local_path  VARCHAR(400),
    pdf_available   BOOLEAN DEFAULT 0,
    is_favorite     BOOLEAN DEFAULT 0,           -- 즐겨찾기(★)
    doi             VARCHAR(255),
    citation_count  INT UNSIGNED,
    citation_source VARCHAR(20),
    citation_updated_at DATETIME,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    KEY idx_year          (year),
    KEY idx_source_name   (source_name),
    KEY idx_source_type   (source_type),
    KEY idx_source_system (source_system),
    KEY idx_pdf_available (pdf_available),
    KEY idx_favorite      (is_favorite),
    KEY idx_doi           (doi),
    KEY idx_citation_count (citation_count),
    FULLTEXT idx_search   (title, authors)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- IEEE/Optica/Nature 논리 분리 VIEW (물리 테이블은 papers 그대로, 읽기 전용 조회용).
-- source_system 컬럼(import_excel_to_db.py가 URL 패턴으로 명시적으로 채움) 기준
-- — article_number의 숫자 여부만으로 추론하던 예전 방식은 Nature 키(s41566-...)도
-- 비숫자라 Optica와 충돌해서 source_system 컬럼으로 교체함.
-- 웹서버의 추천(/api/recommendations), 퍼블리셔 필터 등에서 사용하고, 기존
-- 스크립트(import/download/zotero 등)는 계속 papers 테이블에 직접 쓰기/읽기 —
-- 마이그레이션 없이 되돌리기도 DROP VIEW 로 간단함.
CREATE OR REPLACE VIEW papers_ieee AS
    SELECT * FROM papers WHERE source_system = 'ieee';

CREATE OR REPLACE VIEW papers_optica AS
    SELECT * FROM papers WHERE source_system = 'optica';

CREATE OR REPLACE VIEW papers_nature AS
    SELECT * FROM papers WHERE source_system = 'nature';

CREATE OR REPLACE VIEW papers_designcon AS
    SELECT * FROM papers WHERE source_system = 'designcon';

-- SerDes 성능 수치, 초록 snapshot, 필드별 근거는 papers와 분리한다.
-- 기존 DB에는 scripts/migrations/001_serdes_measurements.sql을 실행한다.
-- 신규 설치에서도 같은 additive schema가 생성되도록 migration 파일의 DDL을
-- 이 파일 실행 후 별도로 적용한다.
