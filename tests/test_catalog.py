# (Move file from src/boringcatalog/test_catalog.py to tests/test_catalog.py) 
import os
import subprocess
import sys
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

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

def test_cli_workflow(tmp_catalog_dir, dummy_parquet):
    # 1. Create warehouse directory and run ice init with --warehouse
    warehouse_dir = tmp_catalog_dir / "warehouse"
    warehouse_dir.mkdir()
    result = run_cli(["init", "--warehouse", str(warehouse_dir)], cwd=tmp_catalog_dir)
    assert result.returncode == 0
    assert (tmp_catalog_dir / ".ice" / "index").exists()

    # 2. ice commit my_table --source dummy.parquet
    result = run_cli([
        "commit", "my_table", "--source", str(dummy_parquet)
    ], cwd=tmp_catalog_dir)
    assert result.returncode == 0
    assert "Committed" in result.stdout

    # 3. ice log my_table
    result = run_cli(["log", "my_table"], cwd=tmp_catalog_dir)
    assert result.returncode == 0
    assert "commit" in result.stdout

    # 4. ice catalog (should print catalog json)
    result = run_cli(["catalog"], cwd=tmp_catalog_dir)
    assert result.returncode == 0
    assert '"tables"' in result.stdout

    # 5. ice duck (just check it starts, don't wait for interactive)
    # We'll skip actually running duckdb interactively in CI
    # result = run_cli(["duck"], cwd=tmp_catalog_dir)
    # assert result.returncode == 0 