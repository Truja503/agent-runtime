"""Privileged domain.

A separate security boundary. Nothing in here may import from ``app``: the
package is meant to be runnable as its own process, under its own Unix user,
reachable only through a narrow interface.
"""
