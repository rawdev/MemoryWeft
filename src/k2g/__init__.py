"""
K2G - Knowledge to Graph
Multi-dimensional knowledge graph engine: transforms text/code/multimedia into structured graphs.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("k2g")
except PackageNotFoundError:
    __version__ = "0.1.0-dev"

__all__ = ["__version__"]
