# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,google]"
ruff format . && ruff check . && mypy && pytest
```

- **Tests never call real APIs.** Use the `FakeWeb` fixture in `tests/conftest.py` (`httpx.MockTransport`).
- **New integrations** must link the official documentation in `docs/engines.md`, with the date it was checked, and must default to dry run.
- **Don't widen what an API is used for.** If a provider restricts an endpoint (like the Google Indexing API), the submitter must enforce that restriction.
- Keep secrets out of arguments and logs; add a test when touching anything that logs URLs.
