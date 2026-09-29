"""Protected reconciliation oracle.

This package is trusted validation code. It must never be imported by agent or tool code
(enforced by ``tests/test_isolation.py``) and it never reads dbt outputs: it recomputes the
expected business results directly from the immutable, checksummed source fixtures.
"""
