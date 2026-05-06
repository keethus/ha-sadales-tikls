# Contributing

Contributions are welcome — bug reports, fixes, new sensors, translations,
docs improvements. The bar is "make the integration more useful for people
running Home Assistant against a Sadales Tīkls APIKEY".

## Quick start

```bash
git clone https://github.com/keethus/ha-sadales-tikls.git
cd ha-sadales-tikls
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest          # tests
mypy            # type check (strict)
ruff check .    # lint
ruff format .   # format
```

`pytest`, `mypy --strict`, and `ruff check` must all pass before a PR can land.

## Pull requests

1. Fork and create a branch from `main`.
2. Keep changes focused — one logical change per PR.
3. Add or update tests for behavior changes. The integration ships with
   `mypy --strict`, so type annotations are required.
4. Update [README.md](README.md) if user-visible behavior changed.
5. Open the PR; CI runs hassfest, the HACS Action, and the tests.

## Bug reports

Use [GitHub Issues](https://github.com/keethus/ha-sadales-tikls/issues).
Helpful reports include:

- Home Assistant version
- Integration version (from `manifest.json`)
- A redacted snippet from **Settings → System → Logs** at DEBUG level
  (set via `logger:` config — see the README troubleshooting section).
  **Never paste your APIKEY** — the integration is careful not to log it,
  but redact anyway.
- Steps to reproduce.

## Secrets

The Sadales Tīkls APIKEY is the only secret in this integration. The client
in [`api.py`](custom_components/sadales_tikls/api.py) deliberately never logs
it. If you add new logging, keep that invariant — there's a regression test
(`test_api_key_never_logged`) that asserts it.

## License

By contributing you agree that your contributions will be licensed under the
[MIT License](LICENSE).
