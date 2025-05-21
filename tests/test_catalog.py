# (Move file from src/boringcatalog/test_catalog.py to tests/test_catalog.py) 
import os
import subprocess
import sys
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
import json
from boringcatalog import BoringCatalog
import shutil
import logging

@pytest.fixture(scope="function")
def tmp_catalog_dir(tmp_path):
    return tmp_path

@pytest.fixture(scope="function")
def dummy_parquet(tmp_path):
    # Create a small dummy parquet file
    df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    table = pa.Table.from_pandas(df)
    parquet_path = tmp_path / "dummy.parquet"
    pq.write_table(table, parquet_path)
    return parquet_path

def run_cli(args, cwd):
    cmd = [sys.executable, "-m", "boringcatalog.cli"] + args
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    print("STDOUT:\n", result.stdout)
    print("STDERR:\n", result.stderr)
    return result

@pytest.mark.parametrize("args,expected_index,do_workflow", [
    # ice init (no args)
    ([], {
        "catalog_uri": "warehouse/catalog/catalog_boring.json",
        "properties": {"warehouse": "warehouse"}
    }, True),
    # ice init -p warehouse=warehouse3/
    (["-p", "warehouse=warehouse3"], {
        "catalog_uri": "warehouse3/catalog/catalog_boring.json",
        "properties": {"warehouse": "warehouse3"}
    }, True),
    # ice init --catalog warehouse2/catalog_boring.json
    (["--catalog", "warehouse2/catalog_boring.json"], {
        "catalog_uri": "warehouse2/catalog_boring.json",
        "properties": {}
    }, False),
    # ice init --catalog tt/catalog.json -p warehouse=warehouse4
    (["--catalog", "tt/catalog.json", "-p", "warehouse=warehouse4"], {
        "catalog_uri": "tt/catalog.json",
        "properties": {"warehouse": "warehouse4"}
    }, False),
    # ice init -p warehouse=tttrr
    (["-p", "warehouse=tttrr"], {
        "catalog_uri": "tttrr/catalog/catalog_boring.json",
        "properties": {"warehouse": "tttrr"}
    }, False),
])
def test_ice_init_variants(tmp_path, args, expected_index, do_workflow, caplog):
    # Clean up .ice if it exists
    ice_dir = tmp_path / ".ice"
    if ice_dir.exists():
        shutil.rmtree(ice_dir)
    # If warehouse is needed, create it
    warehouse = expected_index["properties"].get("warehouse")
    if warehouse:
        warehouse_dir = tmp_path / warehouse
        warehouse_dir.mkdir(parents=True, exist_ok=True)
    # Run CLI
    result = run_cli(["init"] + args, cwd=tmp_path)
    assert result.returncode == 0
    index_path = tmp_path / ".ice" / "index"
    assert index_path.exists(), f".ice/index not created for args {args}"
    # Check content
    with open(index_path) as f:
        index = json.load(f)
    assert index["catalog_uri"] == expected_index["catalog_uri"], f"catalog_uri mismatch for args {args}"
    # Only check properties equality if warehouse is specified in expected_index
    if expected_index["properties"]:
        assert index["properties"] == expected_index["properties"], f"properties mismatch for args {args}"
    # Check BoringCatalog usage
    os.chdir(tmp_path)
    caplog.set_level(logging.INFO)
    catalog = BoringCatalog()
    # If warehouse is not specified, it should default to the catalog folder
    if not expected_index["properties"].get("warehouse"):
        expected_warehouse = str(os.path.dirname(index["catalog_uri"]))
        assert catalog.properties["warehouse"] == expected_warehouse
        assert f"Using catalog folder as warehouse: {expected_warehouse}" in caplog.text
    namespaces = catalog.list_namespaces()
    assert isinstance(namespaces, list)
    # If do_workflow, run commit, log, catalog commands
    if do_workflow:
        # Create dummy parquet
        df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
        table = pa.Table.from_pandas(df)
        parquet_path = tmp_path / "dummy.parquet"
        pq.write_table(table, parquet_path)
        # ice commit my_table --source dummy.parquet
        result = run_cli([
            "commit", "my_table", "--source", str(parquet_path)
        ], cwd=tmp_path)
        assert result.returncode == 0
        assert "Committed" in result.stdout
        # ice log my_table
        result = run_cli(["log", "my_table"], cwd=tmp_path)
        assert result.returncode == 0
        assert "commit" in result.stdout
        # ice catalog
        result = run_cli(["catalog"], cwd=tmp_path)
        assert result.returncode == 0
        assert '"tables"' in result.stdout

    # 5. ice duck (just check it starts, don't wait for interactive)
    # We'll skip actually running duckdb interactively in CI
    # result = run_cli(["duck"], cwd=tmp_catalog_dir)
    # assert result.returncode == 0 

def test_custom_catalog_name(tmp_path):
    # Clean up .ice if it exists
    ice_dir = tmp_path / ".ice"
    if ice_dir.exists():
        shutil.rmtree(ice_dir)
    warehouse_dir = tmp_path / "customwarehouse"
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    custom_name = "mycat"
    # Run CLI with custom catalog name
    result = run_cli([
        "init", "-p", "warehouse=customwarehouse", "--catalog-name", custom_name
    ], cwd=tmp_path)
    assert result.returncode == 0
    index_path = tmp_path / ".ice" / "index"
    assert index_path.exists(), ".ice/index not created for custom catalog name"
    with open(index_path) as f:
        index = json.load(f)
    assert index["catalog_uri"].endswith(f"catalog_{custom_name}.json")
    assert index["catalog_name"] == custom_name
    # Check BoringCatalog instance uses the custom name
    os.chdir(tmp_path)
    catalog = BoringCatalog()
    assert catalog.name == custom_name
    # Check catalog.json content
    with open(index["catalog_uri"]) as f:
        catalog_json = json.load(f)
    assert catalog_json["catalog_name"] == custom_name 

def test_custom_catalog_file_path(tmp_path):
    # Test initializing with a fully custom catalog file path
    ice_dir = tmp_path / ".ice"
    if ice_dir.exists():
        shutil.rmtree(ice_dir)
    custom_catalog_path = tmp_path / "mydir" / "mycustom.json"
    custom_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_cli([
        "init", "--catalog", str(custom_catalog_path), "--catalog-name", "specialcat"
    ], cwd=tmp_path)
    assert result.returncode == 0
    index_path = tmp_path / ".ice" / "index"
    assert index_path.exists(), ".ice/index not created for custom catalog path"
    with open(index_path) as f:
        index = json.load(f)
    assert index["catalog_uri"] == str(custom_catalog_path)
    assert index["catalog_name"] == "specialcat"
    assert custom_catalog_path.exists(), "Custom catalog file was not created"
    # Check BoringCatalog loads from this path
    os.chdir(tmp_path)
    catalog = BoringCatalog()
    assert catalog.name == "specialcat"
    assert catalog.uri == str(custom_catalog_path)


def test_reinit_overwrite_behavior(tmp_path):
    # Test running ice init twice in the same directory
    ice_dir = tmp_path / ".ice"
    if ice_dir.exists():
        shutil.rmtree(ice_dir)
    warehouse_dir = tmp_path / "warehouse"
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    # First init
    result1 = run_cli(["init", "-p", "warehouse=warehouse"], cwd=tmp_path)
    assert result1.returncode == 0
    index_path = tmp_path / ".ice" / "index"
    assert index_path.exists()
    with open(index_path) as f:
        index1 = json.load(f)
    # Second init (should overwrite or succeed)
    result2 = run_cli(["init", "-p", "warehouse=warehouse"], cwd=tmp_path)
    # Accept both overwrite and success (should not crash)
    assert result2.returncode == 0
    with open(index_path) as f:
        index2 = json.load(f)
    # The index file should still be valid and point to the same warehouse
    assert index2["properties"]["warehouse"] == "warehouse"


def test_manual_index_loading(tmp_path):
    # Test loading a catalog from a manually created .ice/index file
    ice_dir = tmp_path / ".ice"
    ice_dir.mkdir(exist_ok=True)
    custom_catalog_path = tmp_path / "manualcat.json"
    # Write a minimal catalog file
    with open(custom_catalog_path, "w") as f:
        json.dump({"catalog_name": "manualcat", "namespaces": {}, "tables": {}}, f)
    # Write a custom .ice/index
    index = {
        "catalog_uri": str(custom_catalog_path),
        "catalog_name": "manualcat",
        "properties": {"warehouse": "manualwarehouse"}
    }
    with open(ice_dir / "index", "w") as f:
        json.dump(index, f)
    os.chdir(tmp_path)
    catalog = BoringCatalog()
    assert catalog.name == "manualcat"
    assert catalog.uri == str(custom_catalog_path)
    assert catalog.properties["warehouse"] == "manualwarehouse"
    # Now test missing catalog_name (should default to 'boring')
    index2 = {
        "catalog_uri": str(custom_catalog_path),
        "properties": {"warehouse": "manualwarehouse"}
    }
    with open(ice_dir / "index", "w") as f:
        json.dump(index2, f)
    catalog2 = BoringCatalog()
    assert catalog2.name == "boring"
    assert catalog2.uri == str(custom_catalog_path)
    assert catalog2.properties["warehouse"] == "manualwarehouse" 
