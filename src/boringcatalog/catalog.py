from typing import Dict, List, Optional, Set, Tuple, Union, Any
import uuid
import json
import os
import tempfile
import fsspec
import logging
from pyiceberg.io import load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import CommitTableResponse, Table
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import TableRequirement, TableUpdate
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties
from pyiceberg.types import strtobool
from pyiceberg.catalog import (
    Catalog,
    MetastoreCatalog,
    METADATA_LOCATION,
    PREVIOUS_METADATA_LOCATION,
    TABLE_TYPE,
    ICEBERG,
    PropertiesUpdateSummary,
)
from pyiceberg.exceptions import (
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
    NoSuchPropertyException,
    NoSuchIcebergTableError,
    CommitFailedException,
)


from time import perf_counter
# Set up logging
logger = logging.getLogger(__name__)

DEFAULT_INIT_CATALOG_TABLES = "true"
DEFAULT_CATALOG_NAME = "boring"
class ConcurrentModificationError(CommitFailedException):
    """Raised when a concurrent modification is detected."""
    pass

class BoringCatalog(MetastoreCatalog):
    """A simple file-based Iceberg catalog implementation."""
    
    def __init__(self, name: str = DEFAULT_CATALOG_NAME, **properties: str):
        super().__init__(name, **properties)
        

        if self.properties.get("uri"):
            self.uri = self.properties.get("uri")
        elif  self.properties.get("warehouse"):
            self.uri = os.path.join(os.path.join(self.properties.get("warehouse"), "catalog"), f"catalog_{name}.json")

        elif os.path.exists(os.path.join(os.getcwd(), ".ice/index")):
            with open(os.path.join(os.getcwd(), ".ice/index"), 'r') as f:
                index = json.load(f)
                self.uri = index["catalog_uri"]
                self.properties = index["properties"]
        else:
            ValueError("Either provide 'catalog' or 'warehouse' property to initialize BoringCatalog")
            
        init_catalog_tables = strtobool(self.properties.get("init_catalog_tables", DEFAULT_INIT_CATALOG_TABLES))
        
        if init_catalog_tables:
            self._ensure_tables_exist()

    @property
    def catalog(self):
        catalog, _ = self._read_catalog_json()
        return catalog

    @property
    def latest_snapshot(self, table_identifier: str):
        table = self.load_table(table_identifier)
        io = load_file_io(properties=self.properties, location=self.uri)
        file = io.new_input(metadata_location) 
        return file

    def _ensure_tables_exist(self):
        """Ensure catalog directory and catalog.json exist."""
        try:
            # location = self._resolve_table_location(location, namespace, table_name)
            # from pyiceberg.io import _infer_file_io_from_scheme
            # _infer_file_io_from_scheme(self.uri, self.properties)
            # from pyiceberg.io import _import_file_io
            # _import_file_io(self.uri)
            # location_provider = load_location_provider(table_location=location, table_properties=properties)
            # metadata_location = location_provider.new_table_metadata_file_location()
            io = load_file_io(properties=self.properties, location=self.uri)
            
            # Check if catalog file exists
            input_file = io.new_input(self.uri)
            if not input_file.exists():
                # Create initial catalog structure
                initial_catalog = {
                    "catalog_name": self.name,
                    "namespaces": {},
                    "tables": {}
                }
                
                # Write the initial catalog file
                with io.new_output(self.uri).create(overwrite=True) as f:
                    f.write(json.dumps(initial_catalog, indent=2).encode('utf-8'))

        except Exception as e:
            raise ValueError(f"Failed to initialize catalog at {self.uri}: {str(e)}")

    def _has_catalog_changed(self) -> bool:
        """Check if the catalog file has changed by comparing ETags.
        
        Returns:
            bool: True if the catalog has changed, False otherwise
        """
        try:
            io = load_file_io(properties=self.properties, location=self.uri)
            input_file = io.new_input(self.uri)
            
            if not input_file.exists():
                return True
                
            metadata = input_file.metadata() if hasattr(input_file, 'metadata') else {}
            current_etag = metadata.get("ETag")
            
            has_changed = self.last_etag != current_etag
            if has_changed:
                self.last_etag = current_etag
            return has_changed
            
        except Exception as e:
            logger.warning(f"Error checking catalog ETag: {str(e)}")
            return True  # If we can't check, assume it changed to be safe

    def _read_catalog_json(self):
        """Read catalog.json using FileIO, returning (data, etag)."""
        try:
            io = load_file_io(properties=self.properties, location=self.uri)
            input_file = io.new_input(self.uri)
            
            if not input_file.exists():
                return {"catalog_name": self.name, "namespaces": {}, "tables": {}}, None
                
            with input_file.open() as f:
                data = json.loads(f.read().decode('utf-8'))
            
            # Get metadata for ETag
            metadata = input_file.metadata() if hasattr(input_file, 'metadata') else {}
            etag = metadata.get("ETag")
            return data, etag
            
        except Exception as e:
            if 'No such file' in str(e) or 'not found' in str(e) or '404' in str(e):
                return {"catalog_name": self.name, "namespaces": {}, "tables": {}}, None
            raise

    def _write_catalog_json(self, data, etag=None):
        """Write catalog.json using FileIO, using ETag for concurrency if provided."""
        try:
            io = load_file_io(properties=self.properties, location=self.uri)
            
            # Create output file with ETag check if provided
            output_file = io.new_output(self.uri)
            if etag is not None and hasattr(output_file, 'set_metadata'):
                output_file.set_metadata({"if_match": etag})

            with output_file.create(overwrite=True) as f:
                f.write(json.dumps(data, indent=2).encode('utf-8'))
                
        except Exception as e:
            if 'PreconditionFailed' in str(e) or '412' in str(e):
                raise ConcurrentModificationError("catalog.json was modified concurrently")
            raise

    def _table_key(self, namespace: str, table_name: str) -> str:
        return f"{namespace}.{table_name}"
    
    def create_table(
        self,
        identifier: Union[str, Identifier],
        schema: Union[Schema, "pa.Schema"],
        location: Optional[str] = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
    ) -> Table:
        """Create an Iceberg table."""
        schema: Schema = self._convert_schema_if_needed(schema)  # type: ignore
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)
        table_key = self._table_key(namespace, table_name)

        data, etag = self._read_catalog_json()
        if namespace not in data["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")
        if table_key in data.get("tables", {}):
            raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists")

        location = self._resolve_table_location(location, namespace, table_name)
        location_provider = load_location_provider(table_location=location, table_properties=properties)
        metadata_location = location_provider.new_table_metadata_file_location()

        metadata = new_table_metadata(
            location=location, schema=schema, partition_spec=partition_spec, sort_order=sort_order, properties=properties
        )
        io = load_file_io(properties=self.properties, location=metadata_location)
        self._write_metadata(metadata, io, metadata_location)

        # Add table entry to catalog.json
        if "tables" not in data:
            data["tables"] = {}
        data["tables"][table_key] = {
            "namespace": namespace,
            "name": table_name,
            "metadata_location": metadata_location
        }

        self._write_catalog_json(data, etag)

        return self.load_table(identifier)

    def load_table(self, identifier: Union[str, Identifier], catalog_name: str = None) -> Table:
        """Load the table's metadata and return the table instance using catalog.json."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)
        table_key = self._table_key(namespace, table_name)
        data, _ = self._read_catalog_json()
        table_entry = data.get("tables", {}).get(table_key)
        if not table_entry:
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")
        metadata_location = table_entry["metadata_location"]
        io = load_file_io(properties=self.properties, location=metadata_location)
        file = io.new_input(metadata_location)
        metadata = FromInputFile.table_metadata(file)
        return Table(
            identifier=Catalog.identifier_to_tuple(namespace) + (table_name,),
            metadata=metadata,
            metadata_location=metadata_location,
            io=self._load_file_io(metadata.properties, metadata_location),
            catalog=self
        )

    def drop_table(self, identifier: Union[str, Identifier]) -> None:
        """Drop a table."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)
        table_key = self._table_key(namespace, table_name)
        data, etag = self._read_catalog_json()
        if table_key not in data.get("tables", {}):
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")
        del data["tables"][table_key]
        self._write_catalog_json(data, etag)

    def rename_table(self, from_identifier: Union[str, Identifier], to_identifier: Union[str, Identifier]) -> Table:
        """Rename a table."""
        from_namespace_tuple = Catalog.namespace_from(from_identifier)
        from_namespace = Catalog.namespace_to_string(from_namespace_tuple)
        from_table_name = Catalog.table_name_from(from_identifier)
        from_table_key = self._table_key(from_namespace, from_table_name)

        to_namespace_tuple = Catalog.namespace_from(to_identifier)
        to_namespace = Catalog.namespace_to_string(to_namespace_tuple)
        to_table_name = Catalog.table_name_from(to_identifier)
        to_table_key = self._table_key(to_namespace, to_table_name)

        data, etag = self._read_catalog_json()
        if not self._namespace_exists(to_namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {to_namespace}")
        
        if from_table_key not in data.get("tables", {}):
            raise NoSuchTableError(f"Table does not exist: {from_namespace}.{from_table_name}")
            
        if to_table_key in data.get("tables", {}):
            raise TableAlreadyExistsError(f"Table {to_namespace}.{to_table_name} already exists")

        table_entry = data["tables"][from_table_key]
        table_entry["namespace"] = to_namespace
        table_entry["name"] = to_table_name
        data["tables"][to_table_key] = table_entry
        del data["tables"][from_table_key]
        
        self._write_catalog_json(data, etag)
        return self.load_table(to_identifier)

    def create_namespace(self, namespace: Union[str, Identifier], properties: Properties = EMPTY_DICT) -> None:
        """Create a namespace in the catalog.json file."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, etag = self._read_catalog_json()
        if namespace_str in data["namespaces"]:
            raise NamespaceAlreadyExistsError(f"Namespace already exists: {namespace_str}")
        data["namespaces"][namespace_str] = {"properties": properties or {"exists": "true"}}
        self._write_catalog_json(data, etag)

    def drop_namespace(self, namespace: Union[str, Identifier]) -> None:
        """Drop a namespace from catalog.json."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, etag = self._read_catalog_json()
        if namespace_str not in data["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace_str}")
        if any(tbl["namespace"] == namespace_str for tbl in data["tables"].values()):
            raise NamespaceNotEmptyError(f"Namespace {namespace_str} is not empty.")
        del data["namespaces"][namespace_str]
        self._write_catalog_json(data, etag)

    def list_tables(self, namespace: Union[str, Identifier]) -> List[Identifier]:
        """List tables under the given namespace from catalog.json."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, _ = self._read_catalog_json()
        if namespace_str and namespace_str not in data["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace_str}")
        return [
            Catalog.identifier_to_tuple(tbl["namespace"]) + (tbl["name"],)
            for tbl in data.get("tables", {}).values()
            if tbl["namespace"] == namespace_str
        ]

    def list_namespaces(self, namespace: Union[str, Identifier] = ()) -> List[Identifier]:
        """List namespaces from catalog.json."""
        data, _ = self._read_catalog_json()
        all_namespaces = list(data["namespaces"].keys())
        if not namespace:
            return [Catalog.identifier_to_tuple(ns) for ns in all_namespaces]
        ns_tuple = Catalog.identifier_to_tuple(namespace)
        ns_prefix = Catalog.namespace_to_string(namespace)
        # Only return direct children
        result = []
        for ns in all_namespaces:
            ns_parts = Catalog.identifier_to_tuple(ns)
            if ns_parts[:len(ns_tuple)] == ns_tuple and len(ns_parts) == len(ns_tuple) + 1:
                result.append(ns_parts)
        return result

    def load_namespace_properties(self, namespace: Union[str, Identifier]) -> Properties:
        """Get properties for a namespace from catalog.json."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, _ = self._read_catalog_json()
        if namespace_str not in data["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace {namespace_str} does not exist")
        return data["namespaces"][namespace_str].get("properties", {})

    def _namespace_exists(self, namespace: Union[str, Identifier]) -> bool:
        """Check if a namespace exists in catalog.json."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, _ = self._read_catalog_json()
        return namespace_str in data["namespaces"]

    def _table_exists(self, identifier: Union[str, Identifier]) -> bool:
        """Check if a table exists in catalog.json."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)
        table_key = self._table_key(namespace, table_name)
        data, _ = self._read_catalog_json()
        return table_key in data.get("tables", {})

    def list_views(self, namespace: Union[str, Identifier]) -> List[Identifier]:
        return []

    def drop_view(self, identifier: Union[str, Identifier]) -> None:
        raise NotImplementedError("Views are not supported")

    def view_exists(self, identifier: Union[str, Identifier]) -> bool:
        return False

    def commit_table(
        self, table: Table, requirements: Tuple[TableRequirement, ...], updates: Tuple[TableUpdate, ...]
    ) -> CommitTableResponse:
        """Commit updates to a table."""
        table_identifier = table.name()
        namespace_tuple = Catalog.namespace_from(table_identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(table_identifier)

        current_table: Optional[Table]
        try:
            current_table = self.load_table(table_identifier)
        except NoSuchTableError:
            current_table = None

        updated_staged_table = self._update_and_stage_table(current_table, table.name(), requirements, updates)
        if current_table and updated_staged_table.metadata == current_table.metadata:
            return CommitTableResponse(metadata=current_table.metadata, metadata_location=current_table.metadata_location)

        self._write_metadata(
            metadata=updated_staged_table.metadata,
            io=updated_staged_table.io,
            metadata_path=updated_staged_table.metadata_location,
        )

        try:
            data, etag = self._read_catalog_json()
            table_key = self._table_key(namespace, table_name)
            
            if current_table:
                if data["tables"][table_key]["metadata_location"] != current_table.metadata_location:
                    raise CommitFailedException(f"Table has been updated by another process: {namespace}.{table_name}")
                data["tables"][table_key]["previous_metadata_location"] = current_table.metadata_location
            else:
                if table_key in data["tables"]:
                    raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists")
                data["tables"][table_key] = {
                    "namespace": namespace,
                    "name": table_name,
                    "previous_metadata_location": None
                }
            
            data["tables"][table_key]["metadata_location"] = updated_staged_table.metadata_location
            self._write_catalog_json(data, etag)

        except Exception as e:
            try:
                updated_staged_table.io.delete(updated_staged_table.metadata_location)
            except Exception:
                pass
            raise e

        return CommitTableResponse(
            metadata=updated_staged_table.metadata,
            metadata_location=updated_staged_table.metadata_location
        )

    def register_table(self, identifier: Union[str, Identifier], metadata_location: str) -> Table:
        """Register a new table using existing metadata."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)
        table_key = self._table_key(namespace, table_name)

        if not self._namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        data, etag = self._read_catalog_json()
        if table_key in data.get("tables", {}):
            raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists")

        data["tables"][table_key] = {
            "namespace": namespace,
            "name": table_name,
            "metadata_location": metadata_location,
            "previous_metadata_location": None
        }
        self._write_catalog_json(data, etag)

        return self.load_table(identifier)

    def update_namespace_properties(
        self, namespace: Union[str, Identifier], removals: Optional[Set[str]] = None, updates: Properties = EMPTY_DICT
    ) -> PropertiesUpdateSummary:
        """Remove provided property keys and update properties for a namespace in catalog.json."""
        namespace_str = Catalog.namespace_to_string(namespace)
        data, etag = self._read_catalog_json()
        if namespace_str not in data["namespaces"]:
            raise NoSuchNamespaceError(f"Namespace {namespace_str} does not exist")
        current_properties = data["namespaces"][namespace_str].get("properties", {})
        if removals:
            for key in removals:
                current_properties.pop(key, None)
        if updates:
            current_properties.update(updates)
        data["namespaces"][namespace_str]["properties"] = current_properties
        self._write_catalog_json(data, etag)
        # Return a dummy PropertiesUpdateSummary for now (implement as needed)
        return PropertiesUpdateSummary()
