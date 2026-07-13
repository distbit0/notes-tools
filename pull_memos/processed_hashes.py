import json
import os

class ProcessedHashes:
    def __init__(self, hash_file_path):
        self.hash_file_path = hash_file_path
        self.hashes = self._load_hashes()

    def _load_hashes(self):
        if os.path.exists(self.hash_file_path):
            try:
                with open(self.hash_file_path, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def save_hashes(self):
        with open(self.hash_file_path, 'w') as f:
            json.dump(self.hashes, f, indent=2)

    def is_hash_processed(self, file_hash):
        return file_hash in self.hashes

    def add_hash(self, file_hash, file_info):
        self.hashes[file_hash] = file_info
        self.save_hashes()
