"""A DuckDB-based Iceberg catalog implementation."""

from .catalog import BoringCatalog

__all__ = ["BoringCatalog"]

def hello() -> str:
    return "Hello from boring-catalog!"
