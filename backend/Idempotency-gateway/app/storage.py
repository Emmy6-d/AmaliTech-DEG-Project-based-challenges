import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

def generate_fingerprint(payload: Dict[str, Any]) -> str:
    """
    Generate a SHA-256 fingerprint of the request payload.
    The payload is serialized with sorted keys and no whitespace to ensure consistency.
    """
    compact_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(compact_json.encode('utf-8')).hexdigest()

class IdempotencyRecord:
    def __init__(self, request_hash: str):
        self.request_hash: str = request_hash
        self.created_at: datetime = datetime.now(timezone.utc)
        self.status_code: Optional[int] = None
        self.response_body: Optional[Dict[str, Any]] = None
        self.is_processing: bool = True
        
        # A Future that will resolve with (status_code, response_body) when processing completes
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()

    def is_expired(self, ttl_seconds: float) -> bool:
        """
        Check if the record has lived longer than the allowed TTL.
        """
        elapsed = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return elapsed > ttl_seconds

    def reset_timestamp(self) -> None:
        """
        Reset the creation timestamp to the current time.
        This is typically called after the payment processing completes, ensuring that
        the key's TTL starts from the completion of the payment.
        """
        self.created_at = datetime.now(timezone.utc)


class InMemoryIdempotencyStore:
    def __init__(self, default_ttl_seconds: float = 60.0):
        self._store: Dict[str, IdempotencyRecord] = {}
        self.default_ttl_seconds = default_ttl_seconds

    def get_record(self, key: str) -> Optional[IdempotencyRecord]:
        """
        Retrieve a record by key. If the record exists but has expired,
        it is lazily deleted and None is returned.
        """
        record = self._store.get(key)
        if record:
            if record.is_expired(self.default_ttl_seconds):
                # Only delete if it's finished processing. If it's still in-flight,
                # let it complete to prevent hanging futures.
                if not record.is_processing:
                    self.delete_record(key)
                    return None
            return record
        return None

    def create_record(self, key: str, request_hash: str) -> IdempotencyRecord:
        """
        Register a new idempotency key with an in-flight record.
        """
        record = IdempotencyRecord(request_hash)
        self._store[key] = record
        return record

    def delete_record(self, key: str) -> None:
        """
        Evict a record from the store.
        """
        self._store.pop(key, None)

    def cleanup_expired(self) -> int:
        """
        Manually scan and remove all finished expired records.
        Returns the number of records removed.
        """
        now = datetime.now(timezone.utc)
        keys_to_delete = []
        for key, record in self._store.items():
            if not record.is_processing and record.is_expired(self.default_ttl_seconds):
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            self.delete_record(key)
            
        return len(keys_to_delete)
        
    def clear(self) -> None:
        """
        Wipe the store entirely (useful for testing reset).
        """
        self._store.clear()
