# Usage

## Installation

To use ramses_rf, first install it using pip:

```console
   (.venv) $ pip install ramses-rf
```

## Documentation

We use [sphinx](https://www.sphinx-doc.org/en/master/usage/markdown.html) and
MyST [markup](https://myst-parser.readthedocs.io/en/latest/syntax/organising_content.html) to automatically create this code documentation from `docstr` annotations in our python code.

- Activate your virtual environment for ramses_rf as described in the [Wiki](https://github.com/ramses-rf/ramses_rf/blob/master/README-developers.md).

- Install the extra required dependencies by running ``pip install -r requirements/requirements_docs.txt`` so you can build a local set.

- Then, in a Terminal, enter `cd docs/` and run `sphinx-build -b html source build/html`.

- When the operation finishes, you can open the generated files from the `docs/build/html/` folder in a web browser.
