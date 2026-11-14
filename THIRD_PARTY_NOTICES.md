# Third-party notices

Tacit's own source code is licensed under either of [Apache License 2.0](LICENSE-APACHE) or [MIT](LICENSE-MIT), at your option. That license does **not** cover the third-party components listed here. Each stays under its own license.

## Python runtime dependencies

These are installed from PyPI, not vendored. Almost all are MIT, BSD, Apache-2.0, ISC or PSF licensed. Two have weak-copyleft terms:

| Package | License | Note |
|---|---|---|
| [psycopg](https://www.psycopg.org) (and `psycopg-binary`) | LGPL-3.0 | Used unmodified as a library. If you distribute a build that contains it (for example the Docker image), recipients must be able to replace it, which a normal Python install allows. |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 | Used unmodified. If you change certifi's own files, you must share those changes. |

The exact versions are pinned in `uv.lock`.
