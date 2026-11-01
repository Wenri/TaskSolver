import os
from typing import Self

class KeyChain(object):
    def __init__(self, keys: dict[str, str] | None = None) -> None:
        if keys is None:
            self.keys: dict[str, str] = {}
        else:
            self.keys = keys

    def add_key(self, service: str, key: str) -> Self:
        if os.path.exists(key): # it's a file
            with open(key, "r") as f:
                key = f.readline().strip()
        self.keys.update({service: key})
        return self

    def get_key(self, service: str) -> str:
        if service not in self.keys:
            raise ValueError(f"No keys associated with '{service}'")

        return self.keys[service]

    def __getitem__(self, service: str) -> str:
        return self.get_key(service)
