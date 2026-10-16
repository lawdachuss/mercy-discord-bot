"""Root conftest so tests can import the package as ``cogs.<name>``.

pytest inserts this file's directory into ``sys.path``, which makes the cogs
namespace package importable exactly the way ``main.py`` loads it, no matter
which directory pytest is invoked from.
"""
