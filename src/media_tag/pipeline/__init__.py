"""Bounded producer/consumer pipeline for local cleaning, preprocessing, AI tagging and publishing.

Run it as ``python -m media_tag.pipeline --config config/pipeline.toml``. The
package is import-light on purpose: heavy dependencies (httpx, aiosqlite) are only
required when the orchestrator is actually started.
"""

__all__ = ["PipelineConfig", "load_config", "main"]


def __getattr__(name: str):
    if name in __all__:
        from . import config as _config

        if name == "main":
            from .main import main as _main

            return _main
        return getattr(_config, name)
    raise AttributeError(name)
