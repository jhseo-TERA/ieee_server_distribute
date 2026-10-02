-- IEEE Paper Repository - source_system-specific views
-- Run: mysql -u root -p ieee_repo < scripts/create_source_views.sql
USE ieee_repo;

CREATE OR REPLACE VIEW papers_ieee AS
SELECT * FROM papers WHERE source_system = 'ieee';

CREATE OR REPLACE VIEW papers_optica AS
SELECT * FROM papers WHERE source_system = 'optica';

CREATE OR REPLACE VIEW papers_nature AS
SELECT * FROM papers WHERE source_system = 'nature';

CREATE OR REPLACE VIEW papers_designcon AS
SELECT * FROM papers WHERE source_system = 'designcon';
