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
def test_ice_init_variants(tmp_path, args, expected_index, do_workflow):
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
    assert index["properties"] == expected_index["properties"], f"properties mismatch for args {args}"
    # Check BoringCatalog usage
    os.chdir(tmp_path)
    catalog = BoringCatalog()
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