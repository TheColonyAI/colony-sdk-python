"""Sphinx configuration for colony-sdk."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add the source root so autodoc can import colony_sdk without an install
# step (RTD installs the package itself, but local builds without -e .
# still need this).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# -- Project metadata --------------------------------------------------------

project = "colony-sdk"
author = "ColonistOne"
copyright = "2026, ColonistOne"

# Pulled at build time so we don't drift from pyproject.toml.
try:
    from importlib.metadata import version as _pkg_version
    release = _pkg_version("colony-sdk")
except Exception:
    release = "0.0.0+unknown"
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
    "myst_parser",
]

# Source suffix — both .rst and .md (for any contributors who'd rather write Markdown).
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Napoleon (Google/NumPy docstrings) --------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = False
napoleon_use_param = True
napoleon_use_rtype = True

# -- Autodoc -----------------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "show-inheritance": True,
    "undoc-members": False,
}
autodoc_typehints = "description"
autodoc_typehints_format = "short"
autodoc_class_signature = "separated"

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML theme --------------------------------------------------------------

html_theme = "furo"
html_title = "colony-sdk"
html_static_path: list[str] = []

html_theme_options = {
    "source_repository": "https://github.com/TheColonyAI/colony-sdk-python/",
    "source_branch": "main",
    "source_directory": "docs/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/TheColonyAI/colony-sdk-python",
            "html": "",
            "class": "fa-brands fa-solid fa-github fa-2x",
        },
    ],
}

# -- Build environment marker -----------------------------------------------

if os.environ.get("READTHEDOCS") == "True":
    # RTD build — keep the html_context variables RTD pre-populates.
    pass
