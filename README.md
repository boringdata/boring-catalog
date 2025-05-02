# Boring Catalog

A lightweight, file-based Iceberg catalog implementation using a single JSON file (e.g., on S3, local disk, or any fsspec-compatible storage).

## Why Boring Catalog?
- No need to host or maintain a dedicated catalog service
- All Iceberg metadata is managed in a single JSON file (e.g., `catalog.json`)

## How It Works ? 

Boring Catalog stores all Iceberg catalog state in a single JSON file (e.g., `s3://bucket/path/catalog.json`).
- Namespaces and tables are tracked in this file
- S3 conditional writes prevent concurrent modifications when storing catalog on S3

## Installation

```bash
pip install boringcatalog
```

## CLI Usage

The package includes a command-line interface (CLI) tool called `ice` for managing Iceberg catalogs.

### Initialize a Catalog

```bash
ice init my_catalog # store catalog and iceberg data locally
ice init my_catalog --catalog s3://bucket/path/catalog.json # store catalog on s3
ice init -p warehouse=s3://bucket/path/ # store iceberg data path/<table>/ and catalog in path/catalog/catalog.json
ice init --warehouse ... --catalog .. # store catalg and iceberg data in different locations
```

ice init create a .ice/index directory in your current working directory storing your catalog configuration (as you would do when configuring a catalog in pyiceberg).

### Commit a Table

Get first some parquet data:
```bash
curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
````

Start playing (works only with parquet files):
```
ice commit my_table --source /tmp/yellow_tripdata_2023-01.parquet
ice log my_table
```

### Explore your Iceberg with DuckDB
```
ice duck
```

This opens an interactive Duckdb session with pointers to all your tables and namespaces.

```
show;                               -- show all tables               
select * from catalog.namespaces;   -- list namespaces
select * from catalog.tables;       -- list tables
select * from <namespace>.<table>;  -- query iceberg table
```

## Python Usage

### Create a Catalog, Namespace, and Table

```python
from boringcatalog import BoringCatalog

catalog = BoringCatalog(
    "my_catalog"
    **{
        ...
    }
)


# Create namespaces, tables like in PyIceberg
catalog.create_namespace("my_namespace")
catalog.create_table("my_namespace", "my_table")
catalog.load_table("my_namespace.my_table")
```

You can then explore your iceberg with DuckDB:
```
ice duck --catalog path/to/catalog.json
```


