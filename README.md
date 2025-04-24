# Boring Catalog

A DuckDB-based Iceberg catalog implementation.

The catalog is stored as a single .duckdb file in S3, making it lightweight and portable.

## Why Boring Catalog?
- Eliminates the need to host or maintain a dedicated catalog service
- We can store all our Iceberg metadata in a single DuckDB file, including:
  - Catalog metadata
  - Pointers to Iceberg metadata files (via `read_json('s3://...')`)
  - References to Iceberg table data (via `scan_iceberg('s3://...')`)
- Enables easy sharing across teams and environments through simple S3 URLs using `ATTACH '<s3_url>'`
- We can easily expose a FastAPI REST endpoint to enable writes from Snowflake and other external systems

## How It Works

Boring Catalog uses S3 conditional PUT operations to synchronize the catalog across multiple clients, effectively preventing race conditions during concurrent access.

## Installation

```bash
pip install boringcatalog
```

## Usage

### Create namespace and table

```python
from boringcatalog import BoringCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, StringType, DecimalType
from pyiceberg.schema import NestedField

catalog = BoringCatalog(
    "my_catalog",
    warehouse="s3://{your-bucket}/boringcatalog"
)

if ("my_namespace",) not in catalog.list_namespaces():
    catalog.create_namespace("my_namespace")

schema = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "data", StringType()),
    NestedField(3, "amount", DecimalType(5, 1))
)

if ("my_namespace", "my_table_2") not in catalog.list_tables():
    table = catalog.create_table(
        identifier=("my_namespace", "my_table_2"),
        schema=schema,
        properties={"write.format.default": "parquet"}
    )
```


### Append data

```python
from boringcatalog import BoringCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, StringType, DecimalType
from pyiceberg.schema import NestedField

catalog = BoringCatalog(
    "my_catalog",
    warehouse="s3://{your-bucket}/boringcatalog"
)

table = catalog.load_table(("my_namespace", "my_table_2"))

dummy_data = pd.DataFrame({
    "id": pd.Series(range(1, 10001), dtype="Int32"), 
    "data": [f"Transaction_{i}" for i in range(1, 10001)],
    "amount": [Decimal(str(min(i * 10.5, 9999.9))).quantize(Decimal('0.1')) for i in range(1, 10001)]   
})

arrow_table = pa.Table.from_pandas(
    dummy_data,
    schema=pa.schema([
        ('id', pa.int32(), False), 
        ('data', pa.string(), True),
        ('amount', pa.decimal128(5, 1), True) 
    ]),
    safe=True
)

table.append(arrow_table)
```

Next steps:
[] Reflect tables in the catalog (CREATE VIEW AS SELECT * FROM READ_ICEBERG())
[] Reflect snapshots in a catalog table (CREATE TABLE snapshots as read_json())
[] Improve performance (sync of .duckdb from local to s3 takes too long)
[] Add fastAPI on top of the catalog to allow write from Snowflake and other clients
