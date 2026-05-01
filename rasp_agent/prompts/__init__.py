from importlib.resources import files


def load(name: str) -> str:
    return files(__package__).joinpath(name).read_text()
