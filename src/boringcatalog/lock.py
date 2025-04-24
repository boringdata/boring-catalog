import os
import uuid
import json
import time
from typing import Dict, Optional

class CatalogLock:
    
    """Implements locking using a single S3 file with conditional writes."""
    
    def __init__(self, s3_client, catalog_uri: str, retry_count: int = 3, retry_interval_ms: int = 1000):
        """Initialize the catalog lock.
        
        Args:
            s3_client: S3 filesystem client
            catalog_uri: Full S3 URI for the catalog file (e.g. s3://bucket/path/to/catalog.json)
            retry_count: Number of times to retry lock acquisition
            retry_interval_ms: Interval between retries in milliseconds
        """
        self.s3 = s3_client
        if not catalog_uri.startswith("s3://"):
            raise ValueError("catalog_uri must start with s3://")
        # Use a separate lock.json file in the same directory as the catalog
        self.lock_uri = os.path.join(os.path.dirname(catalog_uri), "lock.json")
        self.node_id = str(uuid.uuid4())
        self.is_locked = False
        self.validity_ms = 60000  # Lock validity period: 60 seconds
        self.retry_count = retry_count
        self.retry_interval_ms = retry_interval_ms
        self._ensure_lock_file()

    def _ensure_lock_file(self):
        """Ensure the lock file exists with initial content."""
        try:
            if not self.s3.exists(self.lock_uri):
                initial_content = {
                    "holder_id": None,
                    "timestamp": None,
                    "validity_ms": self.validity_ms,
                }
                with self.s3.open(self.lock_uri, 'wb') as f:
                    f.write(json.dumps(initial_content).encode('utf-8'))
        except Exception as e:
            print(f"Error ensuring lock file: {str(e)}")

    def _is_lock_expired(self, lock_data: dict) -> bool:
        """Check if a lock has expired."""
        if not lock_data.get("holder_id"):
            return True
            
        timestamp = lock_data.get("timestamp")
        if not timestamp:
            return True
            
        current_time = int(time.time() * 1000)
        return current_time - timestamp > lock_data.get("validity_ms", self.validity_ms)

    def try_acquire_lock(self, operation: str = "Operation") -> bool:
        """Try to acquire the lock using S3 conditional writes with retries.
        
        Args:
            operation: Name of the operation for error messages
            
        Returns:
            bool: True if lock was acquired, False otherwise
            
        Raises:
            ConcurrentModificationError: If lock could not be acquired after retries
        """
        for attempt in range(self.retry_count):
            try:
                # Read current lock state
                with self.s3.open(self.lock_uri, 'rb') as f:
                    lock_data = json.loads(f.read().decode('utf-8'))
                    current_etag = self.s3.info(self.lock_uri)["ETag"]

                # Check if current lock is valid
                if not self._is_lock_expired(lock_data):
                    if attempt < self.retry_count - 1:
                        time.sleep(self.retry_interval_ms / 1000.0)
                        continue
                    return False

                # Try to acquire lock with conditional write
                lock_data.update({
                    "holder_id": self.node_id,
                    "timestamp": int(time.time() * 1000),
                    "validity_ms": self.validity_ms
                })

                try:
                    with self.s3.open(self.lock_uri, 'wb', if_match=current_etag) as f:
                        f.write(json.dumps(lock_data).encode('utf-8'))
                    self.is_locked = True
                    return True

                except Exception as e:
                    if 'PreconditionFailed' in str(e):
                        if attempt < self.retry_count - 1:
                            time.sleep(self.retry_interval_ms / 1000.0)
                            continue
                        return False
                    raise

            except Exception as e:
                print(f"Error acquiring lock: {str(e)}")
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_interval_ms / 1000.0)
                    continue
                return False

        raise ConcurrentModificationError(f"{operation} requires lock")

    def refresh_lock(self) -> bool:
        """Refresh the lock if we still hold it."""
        if not self.is_locked:
            return False

        try:
            # Read current state
            with self.s3.open(self.lock_uri, 'rb') as f:
                lock_data = json.loads(f.read().decode('utf-8'))
                current_etag = self.s3.info(self.lock_uri)["ETag"]

            # Verify we still own the lock
            if lock_data.get("holder_id") != self.node_id:
                self.is_locked = False
                return False

            # Update lock timestamp
            lock_data["timestamp"] = int(time.time() * 1000)

            # Conditional write
            with self.s3.open(self.lock_uri, 'wb', if_match=current_etag) as f:
                f.write(json.dumps(lock_data).encode('utf-8'))
            return True

        except Exception as e:
            print(f"Error refreshing lock: {str(e)}")
            self.is_locked = False
            return False

    def release_lock(self):
        """Release the lock if we hold it."""
        if not self.is_locked:
            return

        try:
            # Read current state
            with self.s3.open(self.lock_uri, 'rb') as f:
                lock_data = json.loads(f.read().decode('utf-8'))
                current_etag = self.s3.info(self.lock_uri)["ETag"]

            # Clear lock
            lock_data.update({
                "holder_id": None,
                "timestamp": int(time.time() * 1000),  # Keep timestamp for tracking
                "validity_ms": self.validity_ms
            })

            # Conditional write
            with self.s3.open(self.lock_uri, 'wb', if_match=current_etag) as f:
                f.write(json.dumps(lock_data).encode('utf-8'))

        except Exception as e:
            print(f"Error releasing lock: {str(e)}")

        finally:
            self.is_locked = False 