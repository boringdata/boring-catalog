-- Install and load extensions
SET VARIABLE catalog_json = '${CATALOG_JSON}';
SET VARIABLE tmp_file = '/tmp/iceberg_init.sql';

.mode list
.header off 
SELECT 'Loading extensions...';
INSTALL iceberg;
LOAD iceberg;

SELECT 'Init schemas and tables...' ;
-- Create schemas
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE TABLE catalog.namespaces AS 
SELECT 
    namespace, 
    unnest(properties.properties)
FROM (
    UNPIVOT (
        SELECT unnest(namespaces) 
        FROM read_json(getvariable('catalog_json'))
    ) ON COLUMNS(*) INTO name namespace value properties
);

CREATE OR REPLACE TABLE catalog.namespaces AS 
SELECT 
    namespace,
    unnest(properties.properties)
FROM (
    UNPIVOT (
        SELECT 
        unnest(namespaces)
        FROM read_json(getvariable('catalog_json')) 
    ) ON COLUMNS(*) INTO name namespace value properties
);
CREATE OR REPLACE TABLE catalog.tables AS 
SELECT
    properties.namespace as namespace,
    table_name as table_name,
    unnest(properties)
FROM (
    UNPIVOT (
        SELECT unnest(tables) 
        FROM read_json(getvariable('catalog_json'))
    ) ON COLUMNS(*) INTO name table_name value properties
);


.mode list
.header off
.once getvariable("tmp_file")
select 'CREATE SCHEMA IF NOT EXISTS ' || i || ';' 
from (select namespace from catalog.namespaces) x(i);
.read getvariable("tmp_file")


.mode list
.header off
.once getvariable("tmp_file")
select 'CREATE OR REPLACE VIEW ' || j || ' AS SELECT * FROM iceberg_scan(''' || k || ''');' 
from (select table_name, metadata_location from catalog.tables) x(j,k);
.read getvariable("tmp_file")

.mode list
.header off
.once getvariable("tmp_file")
select 'CREATE TABLE ' || j || '_metadata AS SELECT * FROM iceberg_metadata(''' || k || ''');' 
from (select table_name, metadata_location from catalog.tables) x(j,k);
.read getvariable("tmp_file")

.mode list
.header off
.once getvariable("tmp_file")
select 'CREATE TABLE ' || j || '_snapshots AS SELECT unnest(snapshots, recursive:=true) from read_json(''' || k || ''');' 
from (select table_name, metadata_location from catalog.tables) x(j,k);

.read getvariable("tmp_file")

SELECT '' ;
SELECT 'Everything is ready! ' ;
SELECT '' ;
SELECT 'Here are some commands to help you get started:' ;
SELECT ' > show;                               -- show all tables' ;
SELECT ' > select * from catalog.namespaces;   -- list namespaces' ;
SELECT ' > select * from catalog.tables;       -- list tables' ;
SELECT ' > select * from <namespace>.<table>;  -- query iceberg table' ;

SELECT '' ;

.mode duckbox
.prompt 'ice ➜ ' 