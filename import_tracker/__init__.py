"""
This top-level module conditionally imports some other sub modules in a way that
tracks their third party deps
"""

# Local
from . import setup_tools
from .import_tracker import (
    ImportEntry,
    ImportReport,
    MissingRef,
    track_imports,
    track_module,
)
from .lazy_import_errors import lazy_import_errors
