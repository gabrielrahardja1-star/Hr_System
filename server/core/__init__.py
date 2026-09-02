"""Domain logic: attendance windows, hours, status coding, recompute, export.

Nothing in here imports FastAPI. It operates on a SQLAlchemy Session and plain
values so it can be driven from scripts, tests, or the web layer identically.
"""
