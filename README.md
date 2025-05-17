# Boring Catalog

A lightweight, file-based Iceberg catalog implementation using a single JSON file (e.g., on S3, local disk, or any fsspec-compatible storage).

## Why Boring Catalog?
- No need to host or maintain a dedicated catalog service
- All catalog state is managed in a single JSON file (e.g., `catalog.json`)
- Easy to use, easy to understand, perfect to get started with Iceberg
- DuckDB CLI interface to easily explore your iceberg tables and metadata

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
ice init
```

ice init create a .ice/index directory in your current working directory storing your catalog configuration (as you would do when configuring a catalog in pyiceberg).

You can also specify a remote warehouse location:

```bash
ice init -p warehouse=s3://bucket/warehouse/ # store iceberg data path/<table>/ and catalog in path/catalog/catalog.json
```

You can configure your Iceberg catalog in several ways depending where you want to store your catalog and iceberg data:

| Command Example | Catalog File Location | Warehouse/Data Location | Use Case |
|-----------------|----------------------|------------------------|----------|
| `ice init` | `warehouse/catalog/catalog_boring.json` | `warehouse/` | Local, simple |
| `ice init -p warehouse=...` | `<warehouse>/catalog/catalog_boring.json` | `<warehouse>/` | Custom warehouse |
| `ice init --catalog ...` | `<custom>.json` | (to define when creating a table) | Custom catalog file |
| `ice init --catalog ... -p warehouse=...` | `<custom>.json` | `<warehouse>/` | Full control |

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

If no namespace is specified, a default namespace is created and used. 
If the table does not exist, it is created.

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

catalog = BoringCatalog() # auto detect .ice/index catalog
```


or specify a catalog:
```python
catalog = BoringCatalog(catalog_uri="path/to/catalog.json")
```

You can then interact with your iceberg catalog as you would do with pyiceberg:

```python
catalog.create_namespace("my_namespace")
catalog.create_table("my_namespace", "my_table")
catalog.load_table("my_namespace.my_table")

df = pq.read_table("/tmp/yellow_tripdata_2023-01.parquet")
table = catalog.load_table(("ice_default", "my_table"))
table.append(df)
```

## Roadmap

- [ ] Improve CLI to allow MERGE operation, partition spec, etc.
- [ ] Expose REST API for integration with AWS, Snowflake, etc.
