import os
import pathlib

from starlette.config import Config

# Current file: src/config/core.py
current_file = pathlib.Path(__file__).resolve()
# parent: src/config/
# parent.parent: src/
# parent.parent.parent: . (root)
ROOT_DIR = current_file.parent.parent.parent

env_file = ROOT_DIR / ".env"
# fallback to src/.env if root .env not found
if not env_file.exists():
    env_file = ROOT_DIR / "src" / ".env"

if env_file.exists():
    starlette_config = Config(str(env_file))
else:
    starlette_config = Config()

# Alias for backward compatibility
config = starlette_config
