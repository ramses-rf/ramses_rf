#!/usr/bin/env python3
# Configuration file for the ramses_rf Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys

sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.abspath("../../src"))

from ramses_rf import VERSION  # noqa: E402

version = VERSION

# -- Project information -----------------------------------------------------
# see https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "Ramses RF"
copyright = "2025, David Bonnes, Egbert Broerse"
author = "David Bonnes, Egbert Broerse"
release = version

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration
# adapted following https://medium.com/@cissyshu/a-step-by-step-guide-to-automatic-documentation-using-sphinx-a697dbbce0e7

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",  # link to local code (button)
    "sphinx.ext.githubpages",
    "sphinx_design",
    "myst_parser",  # to use Markdown inside reST
]
pygments_style = "sphinx"  # enable syntax highlighting

templates_path = ["_templates"]
exclude_patterns = []

# Language
language = "en"

# -- Options for HTML / Thema output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "furo"
html_static_path = ["_static"]

# --- Extension config ----

# Autodoc
autodoc_default_options = {
    # Autodoc members
    "members": True,
    # Autodoc private members
    "private-members": True,
}

# Autosummary
autosummary_generate = True
autosummary_generate_overwrite = True

# Myst
myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 4
