# Boring Catalog

**Made by [boringdata.io](https://boringdata.io) — Kickstart your Iceberg journey with our data stack templates.**

<img src="docs/boringdata.png" alt="Boring Data" width="400">

----

A lightweight, file-based Iceberg catalog implementation using a single JSON file (e.g., on S3, local disk, or any fsspec-compatible storage).

## Why Boring Catalog?
- No need to host or maintain a dedicated catalog service
- Easy to use, easy to understand, perfect to get started with Iceberg
- DuckDB CLI interface to easily explore your iceberg tables and metadata

## How It Works
Boring Catalog stores all Iceberg catalog state in a single JSON file:
- Namespaces and tables are tracked in this file
- S3 conditional writes prevent concurrent modifications when storing catalog on S3
- The `.ice/index` file in your project directory stores the configuration for your catalog, including:
  - `catalog_uri`: the path to your catalog JSON file
  - `catalog_name`: the logical name of your catalog
  - `properties`: additional properties (e.g., warehouse location)

## Installation
```bash
pip install boringcatalog
```

## Quickstart

### Initialize a Catalog
```bash
ice init
```
This creates `.ice/index` and a default local catalog file in `warehouse/catalog/catalog_boring.json`.

You can also specify a remote catalog file:
```bash
ice init -p warehouse=s3://mybucket/mywarehouse
```
This will store the catalog file and iceberg data in S3.

You can then start using the catalog:
```python
curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
ice commit my_table --source /tmp/yellow_tripdata_2023-01.parquet
```

Have a look at the commit history:
```bash
ice log 
```

### Explore your Iceberg (data and metadata) with DuckDB
```bash
ice duck
```
This opens an interactive DuckDB session with pointers to all your tables and namespaces.

Example DuckDB queries:
```
show;                               -- show all tables               
select * from catalog.namespaces;   -- list namespaces
select * from catalog.tables;       -- list tables
select * from <namespace>.<table>;  -- query iceberg table
```

## Python Usage

```python
from boringcatalog import BoringCatalog

# Auto-detects .ice/index in the current working directory
catalog = BoringCatalog()

# Or specify a catalog
catalog = BoringCatalog(name="mycat", uri="path/to/catalog.json")

# Interact with your iceberg catalog
catalog.create_namespace("my_namespace")
catalog.create_table("my_namespace", "my_table")
catalog.load_table("my_namespace.my_table")

import pyarrow.parquet as pq
df = pq.read_table("/tmp/yellow_tripdata_2023-01.parquet")
table = catalog.load_table(("ice_default", "my_table"))
table.append(df)
```


## Custom Init and Catalog Location

You can configure your Iceberg catalog in several ways, depending on where you want to store your catalog metadata (the JSON file) and your Iceberg data (the warehouse):
- The `warehouse` property determines where your Iceberg tables' data will be stored.
- The `--catalog` option lets you specify the exact path for your catalog JSON file.
- If you use both, the catalog file will be created at the path you specify, and the warehouse will be used for table data.

### Examples
| Command Example | Catalog File Location | Warehouse/Data Location | Use Case |
|-----------------|----------------------|------------------------|----------|
| `ice init` | `warehouse/catalog/catalog_boring.json` | `warehouse/` | Local, simple |
| `ice init -p warehouse=...` | `<warehouse>/catalog/catalog_boring.json` | `<warehouse>/` | Custom warehouse |
| `ice init --catalog ...` | `<custom>.json` | (to define when creating a table) | Custom catalog file |
| `ice init --catalog ... -p warehouse=...` | `<custom>.json` | `<warehouse>/` | Full control |
| `ice init --catalog ... --catalog-name ...` | `<custom>.json` | (to define when creating a table) | Custom name & file |

### Edge Cases & Manual Editing
- **Custom Catalog Name:** By default, the catalog is named `"boring"`, but you can set a custom name with `--catalog-name`. This name is used in the catalog JSON and for file naming if you don't specify a custom path.
- **Re-initialization:** If you run `ice init` multiple times in the same directory, the `.ice/index` file will be overwritten with the new configuration. This is useful if you want to re-point your project to a different catalog, but be aware that it will not migrate or merge any existing data.
- **Manual Editing:** Advanced users can manually edit `.ice/index` to point to a different catalog file or change the catalog name. If you do this, make sure the `catalog_uri` and `catalog_name` fields are consistent with your actual catalog JSON file. If you set a `warehouse` property but do not update `catalog_uri`, Boring Catalog will always use the `catalog_uri` from the index file.

## Roadmap
- [ ] Improve CLI to allow MERGE operation, partition spec, etc.
- [ ] Expose REST API for integration with AWS, Snowflake, etc.
