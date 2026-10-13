import asyncio
import json
import logging
import os
import secrets
import time
from config_store import atomic_write_json

log = logging.getLogger("features")


class FeatureStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.lock = asyncio.Lock()
        self._features: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._features = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._features = [x for x in data if isinstance(x, dict) and "id" in x and "name" in x]
            else:
                self._features = []
        except Exception as e:
            log.warning("failed to load features from %s: %s", self.path, e)
            self._features = []

    def get_features(self) -> list[dict]:
        """Return features list, newest first."""
        return list(reversed(self._features))

    async def add_feature(self, name: str, description: str, author: str) -> dict:
        name_clean = str(name).strip()
        desc_clean = str(description).strip()
        author_clean = str(author).strip() or "anonymous"

        if not (1 <= len(name_clean) <= 100):
            raise ValueError("Feature name must be between 1 and 100 characters")
        if not (1 <= len(desc_clean) <= 2000):
            raise ValueError("Feature description must be between 1 and 2000 characters")
        if len(author_clean) > 50:
            author_clean = author_clean[:50]

        feat = {
            "id": secrets.token_hex(6),
            "name": name_clean,
            "description": desc_clean,
            "author": author_clean,
            "created_at": int(time.time()),
        }

        async with self.lock:
            self._features.append(feat)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, atomic_write_json, self._features, self.path)

        return feat

    async def delete_feature(self, feature_id: str) -> bool:
        async with self.lock:
            idx = next((i for i, f in enumerate(self._features) if f.get("id") == feature_id), None)
            if idx is None:
                return False
            self._features.pop(idx)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, atomic_write_json, self._features, self.path)
            return True
