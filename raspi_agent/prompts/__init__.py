from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=8)
def load(name: str) -> str:
    return files(__package__).joinpath(name).read_text()
