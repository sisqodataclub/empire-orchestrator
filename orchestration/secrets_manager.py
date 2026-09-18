##secrets manager 
# orchestration/secrets_manager.py
import os
import json
from pathlib import Path
from cryptography.fernet import Fernet
from typing import Dict, Optional

class SecretsManager:
    def __init__(self, tenant_root: str, encryption_key: str):
        self.file_path = Path(tenant_root) / "secrets.enc.json"
        self.key = encryption_key.encode()
        self.fernet = Fernet(self.key)

    def _read_raw(self) -> Optional[bytes]:
        if self.file_path.exists():
            return self.file_path.read_bytes()
        return None

    def load(self) -> Dict[str, str]:
        raw = self._read_raw()
        if not raw:
            return {}
        decrypted = self.fernet.decrypt(raw)
        return json.loads(decrypted.decode())

    def save(self, data: Dict[str, str]):
        encrypted = self.fernet.encrypt(json.dumps(data).encode())
        self.file_path.write_bytes(encrypted)

    def get(self, key: str) -> Optional[str]:
        data = self.load()
        return data.get(key)

    def set(self, key: str, value: str):
        data = self.load()
        data[key] = value
        self.save(data)

    def delete(self, key: str):
        data = self.load()
        if key in data:
            del data[key]
            self.save(data)

    def list_keys(self) -> list:
        return list(self.load().keys())
