from typing import Dict, List, Optional, Set, Tuple, Union, Any
import uuid
from time import time
import json
import os
import duckdb
import s3fs
import tempfile
from contextlib import contextmanager
import functools
from pyiceberg.io import AWS_ACCESS_KEY_ID, AWS_REGION, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import CommitTableResponse, Table
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import (
    TableRequirement,
    TableUpdate,
)
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties
from pyiceberg.utils.properties import get_first_property_value

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
from pyiceberg.io import load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table, CommitTableResponse
from pyiceberg.table.metadata import TableMetadata, new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import TableUpdate, TableRequirement
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties
from pyiceberg.serializers import FromInputFile

from .lock import CatalogLock    

import logging
from time import perf_counter

# Constants for DuckDB table structure
COL_IDENTIFIER = "identifier"
COL_NAMESPACE = "namespace"
COL_VERSION = "version"
COL_UPDATED_AT = "updated_at"
COL_CREATED_AT = "created_at"
NAMESPACE_TYPE = "NAMESPACE"
PROPERTY_PREFIX = "p."

# Set up logging
logger = logging.getLogger(__name__)

class ConcurrentModificationError(CommitFailedException):
    """Raised when a concurrent modification is detected."""
    pass

def write_operation(func):
    """Decorator for write operations that need locking and DB sync."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        start_time = perf_counter()
        try:
            if not self.catalog_lock.try_acquire_lock(func.__name__):
                raise ConcurrentModificationError(f"{func.__name__} requires lock")
            lock_time = perf_counter()
            logger.debug(f"{func.__name__}: Lock acquired in {(lock_time - start_time)*1000:.2f}ms")
            
            self._refresh_catalog()
            refresh_time = perf_counter()
            logger.debug(f"{func.__name__}: Catalog refreshed in {(refresh_time - lock_time)*1000:.2f}ms")
            
            temp_path = self._create_local_catalog("catalog")
            local_time = perf_counter()
            logger.debug(f"{func.__name__}: Local catalog created in {(local_time - refresh_time)*1000:.2f}ms")
            
            result = func(self, *args, **kwargs)
            operation_time = perf_counter()
            logger.debug(f"{func.__name__}: Operation completed in {(operation_time - local_time)*1000:.2f}ms")
            return result
        finally:
            try:
                self._push_local_catalog(temp_path)
                push_time = perf_counter()
                logger.debug(f"{func.__name__}: Local catalog pushed in {(push_time - operation_time)*1000:.2f}ms")
                
                self.catalog_lock.release_lock()
                end_time = perf_counter()
                logger.debug(f"{func.__name__}: Lock released in {(end_time - push_time)*1000:.2f}ms")
                logger.debug(f"{func.__name__}: Total operation time: {(end_time - start_time)*1000:.2f}ms")
            except Exception as e:
                logger.error(f"{func.__name__}: Error in cleanup: {str(e)}")
                raise
    return wrapper

def read_operation(func):
    """Decorator for read operations that need latest DB state."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        start_time = perf_counter()
        try:
            self._refresh_catalog()
            refresh_time = perf_counter()
            logger.debug(f"{func.__name__}: Catalog refreshed in {(refresh_time - start_time)*1000:.2f}ms")
            
            result = func(self, *args, **kwargs)
            operation_time = perf_counter()
            logger.debug(f"{func.__name__}: Operation completed in {(operation_time - refresh_time)*1000:.2f}ms")
            logger.debug(f"{func.__name__}: Total operation time: {(operation_time - start_time)*1000:.2f}ms")
            return result
        except Exception as e:
            end_time = perf_counter()
            logger.error(f"{func.__name__}: Operation failed after {(end_time - start_time)*1000:.2f}ms: {str(e)}")
            raise
    return wrapper

class BoringCatalog(MetastoreCatalog):
    """A DuckDB-based Iceberg catalog implementation."""
    
    def __init__(self, name: str, **properties: str):
        super().__init__(name, **properties)
        
        # Get or create warehouse location
        warehouse = properties.get("warehouse")
        if not warehouse:
            raise ValueError("warehouse is required")

        # Set up catalog location
        self.catalog_uri = os.path.join(warehouse, "catalog", "catalog.duckdb")
        
        # Configure S3
        s3_config = {
            "client_kwargs": {"endpoint_url": properties.get("s3.endpoint")} if properties.get("s3.endpoint") else {},
            "key": properties.get("s3.access-key-id"),
            "secret": properties.get("s3.secret-access-key")
        }
        self.s3 = s3fs.S3FileSystem(**{k: v for k, v in s3_config.items() if v is not None})
        
        # Initialize catalog lock
        self.catalog_lock = CatalogLock(
            self.s3,
            self.catalog_uri,
            retry_count=int(properties.get("lock.retry_count", "3")),
            retry_interval_ms=int(properties.get("lock.retry_interval_ms", "1000"))
        )
        
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("CREATE OR REPLACE SECRET secret (TYPE s3, PROVIDER credential_chain);")
        
        # Initialize last known ETag
        self.last_etag = None
        
        if not self.s3.exists(self.catalog_uri):
            self.create_tables()
            temp_path = self._create_local_catalog("memory")
            self._push_local_catalog(temp_path)
        
        self._refresh_catalog()
    
    def _refresh_catalog(self):
        """Refresh local catalog from S3."""
        # Check if catalog has changed
        if not self._has_catalog_changed():
            logger.debug("_refresh_catalog: Catalog unchanged, skipping refresh")
            return
            
        self.conn.execute("USE memory")
        
        self.conn.execute("DETACH DATABASE IF EXISTS catalog")
        
        self.conn.execute(f"ATTACH '{self.catalog_uri}' AS catalog")
        
        self.conn.execute("USE catalog")
        
        logger.debug("_refresh_catalog: Catalog refreshed successfully")

    def _create_local_catalog(self, db_name):
        """Create local catalog."""
        temp_path = os.path.join(tempfile.gettempdir(), f"catalog_temp_{uuid.uuid4()}.duckdb")
        
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        self.conn.execute(f"DETACH DATABASE IF EXISTS local_catalog")
        self.conn.execute(f"ATTACH '{temp_path}' AS local_catalog")
        self.conn.execute(f"COPY FROM DATABASE {db_name} to local_catalog")
        return temp_path

    def _push_local_catalog(self, temp_path):
        """Push local catalog to S3."""
        self.conn.execute("USE memory")
        self.conn.execute(f"DETACH DATABASE IF EXISTS local_catalog")

        with open(temp_path, 'rb') as f_src:
            with self.s3.open(self.catalog_uri, 'wb') as f_dst:
                f_dst.write(f_src.read())
        
        # Update ETag after pushing changes
        try:
            self.last_etag = self.s3.info(self.catalog_uri)["ETag"]
        except Exception as e:
            logger.warning(f"Error updating catalog ETag after push: {str(e)}")
            self.last_etag = None
        
        if os.path.exists(temp_path):
            os.remove(temp_path)

    def create_tables(self) -> None:
        """Create the catalog tables."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iceberg_tables (
                catalog_name VARCHAR(255) NOT NULL,
                table_namespace VARCHAR(255) NOT NULL,
                table_name VARCHAR(255) NOT NULL,
                metadata_location VARCHAR(1000),
                previous_metadata_location VARCHAR(1000),
                PRIMARY KEY (catalog_name, table_namespace, table_name)
            )
        """)
        
        # Create iceberg_namespace_properties table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iceberg_namespace_properties (
                catalog_name VARCHAR(255) NOT NULL,
                namespace VARCHAR(255) NOT NULL,
                property_key VARCHAR(255) NOT NULL,
                property_value VARCHAR(1000) NOT NULL,
                PRIMARY KEY (catalog_name, namespace, property_key)
            )
        """)

    @write_operation
    def create_table(
        self,
        identifier: Union[str, Identifier],
        schema: Union[Schema, "pa.Schema"],
        location: Optional[str] = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
    ) -> Table:
        """Create an Iceberg table.

        Args:
            identifier: Table identifier.
            schema: Table's schema.
            location: Location for the table. Optional Argument.
            partition_spec: PartitionSpec for the table.
            sort_order: SortOrder for the table.
            properties: Table properties that can be a string based dictionary.

        Returns:
            Table: the created table instance.

        Raises:
            AlreadyExistsError: If a table with the name already exists.
            ValueError: If the identifier is invalid, or no path is given to store metadata.
            NoSuchNamespaceError: If the namespace does not exist.
        """
        schema: Schema = self._convert_schema_if_needed(schema)  # type: ignore

        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)

        if not self._namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        location = self._resolve_table_location(location, namespace, table_name)
        location_provider = load_location_provider(table_location=location, table_properties=properties)
        metadata_location = location_provider.new_table_metadata_file_location()

        metadata = new_table_metadata(
            location=location, schema=schema, partition_spec=partition_spec, sort_order=sort_order, properties=properties
        )
        io = load_file_io(properties=self.properties, location=metadata_location)
        self._write_metadata(metadata, io, metadata_location)

        try:
            self.conn.execute("""
                INSERT INTO local_catalog.iceberg_tables (
                    catalog_name,
                    table_namespace,
                    table_name,
                    metadata_location,
                    previous_metadata_location
                ) VALUES (?, ?, ?, ?, ?)
            """, [
                self.name,
                namespace,
                table_name,
                metadata_location,
                None
            ])
        except Exception as e:
            try:
                io.delete(metadata_location)
            except Exception:
                pass
            raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists") from e

        return self.load_table(identifier, "local_catalog")

    @read_operation
    def load_table(self, identifier: Union[str, Identifier], catalog_name: str = "catalog") -> Table:
        """Load the table's metadata and return the table instance.

        You can also use this method to check for table existence using 'try catalog.table() except NoSuchTableError'.
        Note: This method doesn't scan data stored in the table.

        Args:
            identifier (str | Identifier): Table identifier.

        Returns:
            Table: the table instance with its metadata.

        Raises:
            NoSuchTableError: If a table with the name does not exist.
        """
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)

        result = self.conn.execute(f"""
            SELECT * FROM {catalog_name}.iceberg_tables 
            WHERE catalog_name = ? AND table_namespace = ? AND table_name = ?
        """, [self.name, namespace, table_name]).fetchone()

        if not result:
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")

        # Check for expected properties
        if not (metadata_location := result[3]):  # metadata_location is the 4th column
            raise NoSuchTableError(f"Table property {METADATA_LOCATION} is missing")
        if not (table_namespace := result[1]):  # table_namespace is the 2nd column
            raise NoSuchTableError(f"Table property table_namespace is missing")
        if not (table_name := result[2]):  # table_name is the 3rd column
            raise NoSuchTableError(f"Table property table_name is missing")

        io = load_file_io(properties=self.properties, location=metadata_location)
        file = io.new_input(metadata_location)
        metadata = FromInputFile.table_metadata(file)

        return Table(
            identifier=Catalog.identifier_to_tuple(table_namespace) + (table_name,),
            metadata=metadata,
            metadata_location=metadata_location,
            io=self._load_file_io(metadata.properties, metadata_location),
            catalog=self
        )

    @write_operation
    def drop_table(self, identifier: Union[str, Identifier]) -> None:
        """Drop a table."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)

        result = self.conn.execute("""
            DELETE FROM local_catalog.iceberg_tables 
            WHERE catalog_name = ? AND table_namespace = ? AND table_name = ?
            RETURNING *
        """, [self.name, namespace, table_name]).fetchone()

        if not result:
            raise NoSuchTableError(f"Table does not exist: {namespace}.{table_name}")

    @write_operation
    def rename_table(self, from_identifier: Union[str, Identifier], to_identifier: Union[str, Identifier]) -> Table:
        """Rename a table."""
        from_namespace_tuple = Catalog.namespace_from(from_identifier)
        from_namespace = Catalog.namespace_to_string(from_namespace_tuple)
        from_table_name = Catalog.table_name_from(from_identifier)

        to_namespace_tuple = Catalog.namespace_from(to_identifier)
        to_namespace = Catalog.namespace_to_string(to_namespace_tuple)
        to_table_name = Catalog.table_name_from(to_identifier)

        if not self._namespace_exists(to_namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {to_namespace}")

        try:
            result = self.conn.execute("""
                UPDATE local_catalog.iceberg_tables 
                SET table_namespace = ?,
                    table_name = ?
                WHERE catalog_name = ? 
                AND table_namespace = ? 
                AND table_name = ?
                RETURNING *
            """, [
                to_namespace,
                to_table_name,
                self.name,
                from_namespace,
                from_table_name
            ]).fetchone()

            if not result:
                raise NoSuchTableError(f"Table does not exist: {from_namespace}.{from_table_name}")

        except Exception as e:
            raise TableAlreadyExistsError(f"Table {to_namespace}.{to_table_name} already exists") from e

        return self.load_table(to_identifier)

    @write_operation
    def create_namespace(self, namespace: Union[str, Identifier], properties: Properties = EMPTY_DICT) -> None:
        """Create a namespace in the catalog.

        Args:
            namespace (str | Identifier): Namespace identifier.
            properties (Properties): A string dictionary of properties for the given namespace.

        Raises:
            NamespaceAlreadyExistsError: If a namespace with the given name already exists.
        """
        if self._namespace_exists(namespace):
            raise NamespaceAlreadyExistsError(f"Namespace already exists: {namespace}")

        create_properties = properties if properties else {"exists": "true"}
        namespace_str = Catalog.namespace_to_string(namespace)

        try:
            for key, value in create_properties.items():
                self.conn.execute("""
                    INSERT INTO local_catalog.iceberg_namespace_properties (
                        catalog_name,
                        namespace,
                        property_key,
                        property_value
                    ) VALUES (?, ?, ?, ?)
                """, [
                    self.name,
                    namespace_str,
                    key,
                    value
                ])
        except Exception as e:
            raise NamespaceAlreadyExistsError(f"Namespace {namespace_str} already exists") from e

    @write_operation
    def drop_namespace(self, namespace: Union[str, Identifier]) -> None:
        """Drop a namespace.

        Args:
            namespace (str | Identifier): Namespace identifier.

        Raises:
            NoSuchNamespaceError: If a namespace with the given name does not exist.
            NamespaceNotEmptyError: If the namespace is not empty.
        """
        namespace_str = Catalog.namespace_to_string(namespace)
        if not self._namespace_exists(namespace_str):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace_str}")

        if tables := self.list_tables(namespace):
            raise NamespaceNotEmptyError(f"Namespace {namespace_str} is not empty. {len(tables)} tables exist.")

        self.conn.execute("""
            DELETE FROM local_catalog.iceberg_namespace_properties
            WHERE catalog_name = ? AND namespace = ?
        """, [self.name, namespace_str])

    @read_operation
    def list_tables(self, namespace: Union[str, Identifier]) -> List[Identifier]:
        """List tables under the given namespace in the catalog.

        Args:
            namespace (str | Identifier): Namespace identifier to search.

        Returns:
            List[Identifier]: list of table identifiers.

        Raises:
            NoSuchNamespaceError: If a namespace with the given name does not exist.
        """
        if namespace and not self._namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        namespace_str = Catalog.namespace_to_string(namespace)
        results = self.conn.execute("""
            SELECT table_namespace, table_name 
            FROM catalog.iceberg_tables 
            WHERE catalog_name = ? AND table_namespace = ?
        """, [self.name, namespace_str]).fetchall()

        return [(Catalog.identifier_to_tuple(row[0]) + (row[1],)) for row in results]

    @read_operation
    def list_namespaces(self, namespace: Union[str, Identifier] = ()) -> List[Identifier]:
        """List namespaces from the given namespace. If not given, list top-level namespaces from the catalog.

        Args:
            namespace (str | Identifier): Namespace identifier to search.

        Returns:
            List[Identifier]: a List of namespace identifiers.

        Raises:
            NoSuchNamespaceError: If a namespace with the given name does not exist.
        """
        if namespace and not self._namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        table_stmt = self.conn.execute("""
            SELECT DISTINCT table_namespace 
            FROM catalog.iceberg_tables 
            WHERE catalog_name = ? AND table_namespace LIKE ?
        """, [self.name, f"{Catalog.namespace_to_string(namespace) if namespace else ''}%"])

        properties_stmt = self.conn.execute("""
            SELECT DISTINCT namespace 
            FROM catalog.iceberg_namespace_properties 
            WHERE catalog_name = ? AND namespace LIKE ?
        """, [self.name, f"{Catalog.namespace_to_string(namespace) if namespace else ''}%"])

        namespace_tuple = Catalog.identifier_to_tuple(namespace) if namespace else ()
        sub_namespaces_level_length = len(namespace_tuple) + 1

        namespaces = list({
            ns[:sub_namespaces_level_length]
            for ns in {
                Catalog.identifier_to_tuple(ns)
                for ns in set(row[0] for row in table_stmt.fetchall()) | set(row[0] for row in properties_stmt.fetchall())
            }
            if len(ns) >= sub_namespaces_level_length
            and ns[: sub_namespaces_level_length - 1] == namespace_tuple
        })

        return namespaces

    @read_operation
    def load_namespace_properties(self, namespace: Union[str, Identifier]) -> Properties:
        """Get properties for a namespace.

        Args:
            namespace (str | Identifier): Namespace identifier.
            properties (Properties): A string dictionary of properties for the given namespace.

        Returns:
            Properties: Properties for the given namespace.

        Raises:
            NoSuchNamespaceError: If a namespace with the given name does not exist.
        """
        namespace_str = Catalog.namespace_to_string(namespace)
        if not self._namespace_exists(namespace_str):
            raise NoSuchNamespaceError(f"Namespace {namespace_str} does not exist")

        results = self.conn.execute("""
            SELECT property_key, property_value 
            FROM catalog.iceberg_namespace_properties 
            WHERE catalog_name = ? AND namespace = ?
        """, [self.name, namespace_str]).fetchall()

        return {row[0]: row[1] for row in results}

    @read_operation
    def _namespace_exists(self, namespace: Union[str, Identifier]) -> bool:
        """Check if a namespace exists."""
        namespace_tuple = Catalog.identifier_to_tuple(namespace)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        namespace_starts_with = namespace.replace("!", "!!").replace("_", "!_").replace("%", "!%") + ".%"

        tables_result = self.conn.execute("""
            SELECT 1 FROM catalog.iceberg_tables 
            WHERE catalog_name = ? AND (table_namespace = ? OR table_namespace LIKE ?)
            LIMIT 1
        """, [self.name, namespace, namespace_starts_with]).fetchone()

        if tables_result:
            return True

        properties_result = self.conn.execute("""
            SELECT 1 FROM catalog.iceberg_namespace_properties 
            WHERE catalog_name = ? AND (namespace = ? OR namespace LIKE ?)
            LIMIT 1
        """, [self.name, namespace, namespace_starts_with]).fetchone()

        return bool(properties_result)

    @read_operation
    def _table_exists(self, identifier: Union[str, Identifier]) -> bool:
        """Check if a table exists."""
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)

        result = self.conn.execute("""
            SELECT 1 FROM catalog.iceberg_tables 
            WHERE catalog_name = ? AND table_namespace = ? AND table_name = ?
            LIMIT 1
        """, [self.name, namespace, table_name]).fetchone()
        return bool(result)

    def close(self) -> None:
        """Close the catalog."""
        if hasattr(self, 'conn'):
            self.conn.close()

    @read_operation
    def list_views(self, namespace: Union[str, Identifier]) -> List[Identifier]:
        return []

    @write_operation
    def drop_view(self, identifier: Union[str, Identifier]) -> None:
        raise NotImplementedError("Views are not supported")

    @read_operation
    def view_exists(self, identifier: Union[str, Identifier]) -> bool:
        return False

    @write_operation
    def commit_table(
        self, table: Table, requirements: Tuple[TableRequirement, ...], updates: Tuple[TableUpdate, ...]
    ) -> CommitTableResponse:
        """Commit updates to a table.

        Args:
            table (Table): The table to be updated.
            requirements: (Tuple[TableRequirement, ...]): Table requirements.
            updates: (Tuple[TableUpdate, ...]): Table updates.

        Returns:
            CommitTableResponse: The updated metadata.

        Raises:
            NoSuchTableError: If a table with the given identifier does not exist.
            CommitFailedException: Requirement not met, or a conflict with a concurrent commit.
        """
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
            if current_table:
                result = self.conn.execute("""
                    UPDATE local_catalog.iceberg_tables 
                    SET metadata_location = ?,
                        previous_metadata_location = ?
                    WHERE catalog_name = ? 
                    AND table_namespace = ? 
                    AND table_name = ?
                    AND metadata_location = ?
                    RETURNING *
                """, [
                    updated_staged_table.metadata_location,
                    current_table.metadata_location,
                    self.name,
                    namespace,
                    table_name,
                    current_table.metadata_location
                ]).fetchone()

                if not result:
                    raise CommitFailedException(f"Table has been updated by another process: {namespace}.{table_name}")
            else:
                try:
                    self.conn.execute("""
                        INSERT INTO local_catalog.iceberg_tables (
                            catalog_name,
                            table_namespace,
                            table_name,
                            metadata_location,
                            previous_metadata_location
                        ) VALUES (?, ?, ?, ?, ?)
                    """, [
                        self.name,
                        namespace,
                        table_name,
                        updated_staged_table.metadata_location,
                        None
                    ])
                except Exception as e:
                    raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists") from e

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

    @write_operation
    def register_table(self, identifier: Union[str, Identifier], metadata_location: str) -> Table:
        """Register a new table using existing metadata.

        Args:
            identifier Union[str, Identifier]: Table identifier for the table
            metadata_location str: The location to the metadata

        Returns:
            Table: The newly registered table

        Raises:
            TableAlreadyExistsError: If the table already exists
            NoSuchNamespaceError: If namespace does not exist
        """
        namespace_tuple = Catalog.namespace_from(identifier)
        namespace = Catalog.namespace_to_string(namespace_tuple)
        table_name = Catalog.table_name_from(identifier)

        if not self._namespace_exists(namespace):
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        try:
            self.conn.execute("""
                INSERT INTO local_catalog.iceberg_tables (
                    catalog_name,
                    table_namespace,
                    table_name,
                    metadata_location,
                    previous_metadata_location
                ) VALUES (?, ?, ?, ?, ?)
            """, [
                self.name,
                namespace,
                table_name,
                metadata_location,
                None
            ])
        except Exception as e:
            raise TableAlreadyExistsError(f"Table {namespace}.{table_name} already exists") from e

        return self.load_table(identifier)

    @write_operation
    def update_namespace_properties(
        self, namespace: Union[str, Identifier], removals: Optional[Set[str]] = None, updates: Properties = EMPTY_DICT
    ) -> PropertiesUpdateSummary:
        """Remove provided property keys and update properties for a namespace.

        Args:
            namespace (str | Identifier): Namespace identifier.
            removals (Set[str]): Set of property keys that need to be removed. Optional Argument.
            updates (Properties): Properties to be updated for the given namespace.

        Returns:
            PropertiesUpdateSummary: Summary of the updates.

        Raises:
            NoSuchNamespaceError: If a namespace with the given name does not exist.
            ValueError: If removals and updates have overlapping keys.
        """
        namespace_str = Catalog.namespace_to_string(namespace)
        if not self._namespace_exists(namespace_str):
            raise NoSuchNamespaceError(f"Namespace {namespace_str} does not exist")

        current_properties = self.load_namespace_properties(namespace=namespace)
        properties_update_summary = self._get_updated_props_and_update_summary(
            current_properties=current_properties, removals=removals, updates=updates
        )[0]

        if removals:
            self.conn.execute("""
                DELETE FROM local_catalog.iceberg_namespace_properties
                WHERE catalog_name = ? AND namespace = ? AND property_key IN (?)
            """, [self.name, namespace_str, tuple(removals)])

        if updates:
            # Delete existing properties that will be updated
            self.conn.execute("""
                DELETE FROM local_catalog.iceberg_namespace_properties
                WHERE catalog_name = ? AND namespace = ? AND property_key IN (?)
            """, [self.name, namespace_str, tuple(updates.keys())])

            # Insert updated properties
            for key, value in updates.items():
                self.conn.execute("""
                    INSERT INTO local_catalog.iceberg_namespace_properties (
                        catalog_name,
                        namespace,
                        property_key,
                        property_value
                    ) VALUES (?, ?, ?, ?)
                """, [self.name, namespace_str, key, value])

        return properties_update_summary

    def _has_catalog_changed(self) -> bool:
        """Check if the catalog file has changed by comparing ETags.
        
        Returns:
            bool: True if the catalog has changed, False otherwise
        """
        try:
            current_etag = self.s3.info(self.catalog_uri)["ETag"]
            has_changed = self.last_etag != current_etag
            if has_changed:
                self.last_etag = current_etag
            return has_changed
        except Exception as e:
            logger.warning(f"Error checking catalog ETag: {str(e)}")
            return True  # If we can't check, assume it changed to be safe
