from hashlib import sha256
from pathlib import Path


def keep_asset(root: Path, content: bytes) -> str:
    digest = sha256(content).hexdigest()
    path = root / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return digest
