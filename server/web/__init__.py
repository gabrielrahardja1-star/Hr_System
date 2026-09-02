"""Server-rendered review UI (Phase 2).

Jinja2 templates + HTMX partials. No build step, nothing loaded from a CDN —
htmx is vendored in static/. Screens: Monthly Review, Daily Attendance,
Exceptions. The CLI in tools/manage.py does the same operations headless.
"""
