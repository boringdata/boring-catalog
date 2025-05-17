import os
import json
import click
import duckdb
import subprocess
import tempfile
import logging
from string import Template
from .catalog import BoringCatalog
import pyarrow.parquet as pq
import datetime
# Configure logging to display in CLI
logging.basicConfig(
    format='%(message)s',
    level=logging.INFO
)

DEFAULT_NAMESPACE  = "ice_default"
DEFAULT_CATALOG_NAME = "boring"
# Silence pyiceberg logs
logging.getLogger('pyiceberg').setLevel(logging.WARNING)

def ensure_ice_dir():
    """Ensure .ice directory exists and return its path."""
    ice_dir = os.path.abspath('.ice')
    os.makedirs(ice_dir, exist_ok=True)
    return ice_dir

def load_index():
    """Load configuration from .ice/index if it exists."""
    index_path = os.path.join(ensure_ice_dir(), 'index')
    try:
        with open(index_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_index(properties, catalog_uri=None, catalog_name=None):
    """Save configuration to .ice/index with separate catalog_uri, catalog_name, and properties sections."""
    config = {
        "catalog_uri": catalog_uri,
        "catalog_name": catalog_name,
        "properties": properties
    }
    index_path = os.path.join(ensure_ice_dir(), 'index')
    with open(index_path, 'w') as f:
        json.dump(config, f, indent=2)

def get_catalog():
    """Get catalog instance from stored configuration."""
    config = load_index()
    if not config:
        raise click.ClickException(
            "No catalog configuration found. Run 'ice init' first."
        )
    
    properties = config.get("properties", {})
    if config.get("catalog_uri"):
        properties["uri"] = config["catalog_uri"]
    # Use catalog_name from top-level field if present, else default
    catalog_name = config.get("catalog_name", DEFAULT_CATALOG_NAME)
    return BoringCatalog(catalog_name, **properties)

def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('Boring Catalog version 0.2.0')
    ctx.exit()

def get_sql_template():
    """Read the SQL template file."""
    template_path = os.path.join(os.path.dirname(__file__), 'duckdb_init.sql')
    with open(template_path, 'r') as f:
        return Template(f.read())

def print_table_log(catalog, table_identifier, label=None):
    """Print the log (snapshots) for a given table identifier."""
    if not catalog._table_exists(table_identifier):
        return False
    if label:
        click.echo(label)
    table = catalog.load_table(table_identifier)
    snapshots = sorted(table.snapshots(), key=lambda x: x.timestamp_ms, reverse=True)
    if not snapshots:
        click.echo(f"No snapshots found for table {table_identifier}.")
        return False
    for snap in snapshots:
        click.echo(f"commit {snap.snapshot_id:<20}")
        ts = datetime.datetime.utcfromtimestamp(int(snap.timestamp_ms) / 1000).strftime('%Y-%m-%d %H:%M:%S UTC')
        click.echo(f"  Table: {table_identifier:<25}")
        click.echo(f"  Date: {ts:<25}")
        click.echo(f"  Operation: {str(snap.summary.operation):<15}")
        click.echo(f"  Summary:")
        summary = snap.summary.additional_properties
        max_key_len = max(len(str(k)) for k in summary.keys()) if summary else 0
        for k, v in summary.items():
            click.echo(f"  {k.ljust(max_key_len)} : {v}")
        click.echo(f" ")
    return True

@click.group(invoke_without_command=True)
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True, help='Show version and exit')
@click.pass_context
def cli(ctx):
    """Boring Catalog CLI tool.
    
    Run 'ice COMMAND --help' for more information on a command.
    """
    # Show help if no command is provided
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit()

@cli.command()
@click.option('--catalog', help='Custom location for catalog.json (default: warehouse/catalog/catalog_<catalog_name>.json)')
@click.option('--property', '-p', multiple=True, help='Properties in the format key=value')
@click.option('--catalog-name', default=DEFAULT_CATALOG_NAME, show_default=True, help='Name of the catalog (used in file naming and metadata)')
def init(catalog, property, catalog_name):
    """Initialize a new Boring Catalog."""

    try:
        properties = {}
        for prop in property:
            try:
                key, value = prop.split('=', 1)
                properties[key.strip()] = value.strip()
            except ValueError:
                raise click.ClickException(f"Invalid property format: {prop}. Use key=value format")
        
        if not catalog and not "warehouse" in properties:
            catalog = f"warehouse/catalog/catalog_{catalog_name}.json"
            properties["warehouse"] = "warehouse"

        elif not catalog and "warehouse" in properties:
            catalog = f"{properties['warehouse']}/catalog/catalog_{catalog_name}.json"
        
        # Do NOT save catalog_name in properties anymore
        save_index(properties, catalog, catalog_name)

        properties["uri"] = catalog
        catalog_instance = BoringCatalog(catalog_name, **properties)
        
        # Display information in specific order
        click.echo(f"Initialized Boring Catalog in {os.path.join('.ice', 'index')}")
        click.echo(f"Catalog location: {catalog}")
        if "warehouse" in properties:
            click.echo(f"Warehouse location: {properties['warehouse']}")
        click.echo(f"Catalog name: {catalog_name}")

    except Exception as e:
        click.echo(f"Error initializing catalog: {str(e)}", err=True)
        raise click.Abort()

@cli.command(name='list-namespaces')
@click.argument('parent', required=False)
def list_namespaces(parent):
    """List all namespaces or child namespaces of PARENT."""
    try:
        catalog = get_catalog()
        namespaces = catalog.list_namespaces(parent if parent else ())
        
        if not namespaces:
            click.echo("No namespaces found.")
            return

        click.echo("Namespaces:")
        for ns in namespaces:
            click.echo(f"  {'.'.join(ns)}")
    except Exception as e:
        click.echo(f"Error listing namespaces: {str(e)}", err=True)
        raise click.Abort()

@cli.command(name='list-tables')
@click.argument('namespace', required=False)
def list_tables(namespace):
    """List all tables in the specified NAMESPACE, or all tables in all namespaces if not specified."""
    try:
        catalog = get_catalog()
        
        if namespace:
            tables = catalog.list_tables(namespace)
            if not tables:
                click.echo(f"No tables found in namespace '{namespace}'.")
                return
            click.echo(f"Tables in namespace '{namespace}':")
            for table in tables:
                table_name = table[-1]
                click.echo(f"  {table_name}")
        else:
            namespaces = catalog.list_namespaces()
            found_any = False
            for ns_tuple in namespaces:
                ns = ".".join(ns_tuple)
                tables = catalog.list_tables(ns)
                if tables:
                    found_any = True
                    click.echo(f"Tables in namespace '{ns}':")
                    for table in tables:
                        table_name = table[-1]
                        click.echo(f"  {table_name}")
            if not found_any:
                click.echo("No tables found in any namespace.")
    except Exception as e:
        click.echo(f"Error listing tables: {str(e)}", err=True)
        raise click.Abort()

@cli.command()
@click.argument('catalog_path', required=False)
def duck(catalog_path=None):
    """Open DuckDB CLI with catalog configuration. Optionally provide a path to a catalog.json."""
    try:
        if catalog_path:
            properties = {"uri": os.path.abspath(catalog_path)}
            catalog = BoringCatalog(DEFAULT_CATALOG_NAME, **properties)
        else:
            config = load_index()
            if not config:
                raise click.ClickException(
                    "No catalog configuration found. Run 'ice init' first."
                )
            catalog = get_catalog()

        if len(catalog.list_namespaces()) == 0:
            raise click.ClickException("No namespaces found in catalog. Run 'ice create-namespace' to create a namespace.")
        
        if len(catalog.catalog.get("tables", {}).keys()) == 0:
            raise click.ClickException("No tables found in catalog. Run 'ice commit' to create a table.")
        
        # Get SQL template and substitute variables
        template_str = get_sql_template().template
        # Add S3 configuration at the beginning of the script
        if "s3" in catalog.uri:
            s3_config = (
                "mode .lines\n"
                "SELECT 'Loading s3 secrets...' ;\n"
                "CREATE OR REPLACE SECRET secret (TYPE s3, PROVIDER credential_chain);\n"
            )
            # Insert the S3 configuration right after the first comment line
            lines = template_str.split('\n')
            template_str = lines[0] + '\n' + s3_config + '\n'.join(lines[1:])
        template = Template(template_str)

        sql = template.substitute(CATALOG_JSON=catalog.uri)

        # Write the SQL to a temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
            f.write(sql)

        # Start DuckDB CLI with the initialization script
        cmd = ['duckdb', '--init', f.name]
        
        subprocess.run(cmd)

        # Clean up
        os.unlink(f.name)

    except Exception as e:
        click.echo(f"Error starting DuckDB CLI: {str(e)}", err=True)
        raise click.Abort()

@cli.command(name='create-namespace')
@click.argument('namespace', required=True)
@click.option('--property', '-p', multiple=True, help='Properties in the format key=value')
def create_namespace(namespace, property):
    """Create a new namespace in the catalog.
    
    NAMESPACE is the name of the namespace to create (e.g. 'my_namespace' or 'parent.child')
    """
    try:
        catalog = get_catalog()
        
        # Parse properties if provided
        properties = {}
        for prop in property:
            try:
                key, value = prop.split('=', 1)
                properties[key.strip()] = value.strip()
            except ValueError:
                raise click.ClickException(f"Invalid property format: {prop}. Use key=value format")
        
        # Create the namespace
        catalog.create_namespace(namespace, properties)
        click.echo(f"Created namespace: {namespace}")
        if properties:
            click.echo("Properties:")
            for key, value in properties.items():
                click.echo(f"  {key}: {value}")
            
    except Exception as e:
        click.echo(f"Error creating namespace: {str(e)}", err=True)
        raise click.Abort()

# Add a utility function to resolve table identifier with namespace logic

def resolve_table_identifier_with_namespace(catalog, table_identifier):
    """Resolve table identifier to include namespace, creating default if needed (like in commit)."""
    if len(table_identifier.split(".")) == 1:
        namespaces = catalog.list_namespaces()
        if len(namespaces) == 0:
            click.echo(f"No namespace found, creating and using default namespace: {DEFAULT_NAMESPACE}")
            namespace = DEFAULT_NAMESPACE
            catalog.create_namespace(namespace)
        elif len(namespaces) == 1:
            namespace = namespaces[0][0]
        else:
            raise click.ClickException("No namespace specified. Please specify a namespace for the table.")
        table_identifier = f"{namespace}.{table_identifier}"
    return table_identifier

@cli.command(name='commit')
@click.argument('table_identifier', required=True)
@click.option('--source', required=True, help='Parquet file URI to commit as a new snapshot')
@click.option('--mode', default='append', help='Mode to commit the file', type=click.Choice(['append', 'overwrite']))
def commit(table_identifier, source, mode):
    """Commit a new snapshot to a table from a Parquet file."""
    try:
        catalog = get_catalog()
        table_identifier = resolve_table_identifier_with_namespace(catalog, table_identifier)
        df = pq.read_table(source)
        if not catalog.table_exists(table_identifier):
            click.echo(f"Table {table_identifier} does not exist in the catalog. Creating it now...")
            catalog.create_table(table_identifier, schema=df.schema)
        table = catalog.load_table(table_identifier)
        if mode == "append":
            table.append(df)
        elif mode == "overwrite":
            table.overwrite(df)
        else:
            raise click.ClickException(f"Invalid mode: {mode}. Use 'append' or 'overwrite'.")
        click.echo(f"Committed {source} to table {table_identifier}")
    except Exception as e:
        click.echo(f"Error committing file to table: {str(e)}", err=True)
        raise click.Abort()

@cli.command(name='log')
@click.argument('table_identifier', required=False)
def log_snapshots(table_identifier):
    """Print all snapshot entries for a table or all tables in the current catalog or default namespace."""
    try:
        catalog = get_catalog()
        if not table_identifier:
            # Default to the default namespace, create if needed
            namespaces = catalog.list_namespaces()
            if not any(ns[0] == DEFAULT_NAMESPACE for ns in namespaces):
                click.echo(f"No namespace found, creating and using default namespace: {DEFAULT_NAMESPACE}")
                catalog.create_namespace(DEFAULT_NAMESPACE)
            tables = catalog.list_tables(DEFAULT_NAMESPACE)
            if not tables:
                click.echo(f"No tables found in default namespace '{DEFAULT_NAMESPACE}'.")
                return
            found_any = False
            for table in tables:
                table_identifier_full = f"{DEFAULT_NAMESPACE}.{table[-1]}"
                if print_table_log(catalog, table_identifier_full, label=f"=== Log for table: {table_identifier_full} ==="):
                    found_any = True
            if not found_any:
                click.echo("No snapshots found for any table in the default namespace.")
            return
        # If a table_identifier is provided, resolve it as in commit
        table_identifier = resolve_table_identifier_with_namespace(catalog, table_identifier)
        if not catalog._table_exists(table_identifier):
            raise click.ClickException(f"Table {table_identifier} does not exist in the catalog.")
        print_table_log(catalog, table_identifier)
    except Exception as e:
        click.echo(f"Error loading catalog or snapshots: {str(e)}", err=True)
        raise click.Abort()

@cli.command(name='catalog')
def print_catalog():
    """Print the current catalog.json as JSON."""
    try:
        catalog = get_catalog()
        catalog_json = catalog.catalog
        click.echo(json.dumps(catalog_json, indent=2))
    except Exception as e:
        click.echo(f"Error printing catalog: {str(e)}", err=True)
        raise click.Abort()

if __name__ == '__main__':
    cli() 