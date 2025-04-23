# Boring Catalog

A DuckDB-based Iceberg catalog implementation that provides a simple and efficient way to manage Iceberg tables using DuckDB as the backend storage.

## Features

- DuckDB-based catalog storage
- S3 support for table storage
- Concurrent access support with locking mechanism
- Full Iceberg catalog API implementation

## Installation

```bash
pip install boring-catalog
```

## Usage

```python
from boring_catalog import BoringCatalog

# Create a catalog instance
catalog = BoringCatalog(
    name="my_catalog",
    warehouse="s3://my-bucket/warehouse",
    s3_endpoint="http://localhost:9000",  # Optional: for custom S3 endpoints
    s3_access_key_id="access_key",       # Optional: for S3 authentication
    s3_secret_access_key="secret_key"    # Optional: for S3 authentication
)

# Create a namespace
catalog.create_namespace("my_namespace")

# Create a table
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, StringType

schema = Schema(
    LongType(), "id",
    StringType(), "name"
)

table = catalog.create_table(
    identifier=("my_namespace", "my_table"),
    schema=schema
)

# List tables in a namespace
tables = catalog.list_tables("my_namespace")

# Drop a table
catalog.drop_table(("my_namespace", "my_table"))

# Drop a namespace
catalog.drop_namespace("my_namespace")
```

## Requirements

- Python >= 3.10
- DuckDB >= 0.9.0
- s3fs >= 2023.12.0
- pyiceberg >= 0.6.0

## License

MIT
