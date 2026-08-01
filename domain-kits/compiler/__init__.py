"""Domain kit YAML → BlockDef compiler."""

__all__ = ["compile_sheet", "main"]


def __getattr__(name: str):
    if name in __all__:
        from domain_kits.compiler import engine

        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
