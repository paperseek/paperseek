## Summary

- 

## Scope

Select all that apply:

- [ ] CLI
- [ ] Web UI
- [ ] Agent Skill
- [ ] Core search logic
- [ ] Source adapter
- [ ] Deployment
- [ ] Documentation
- [ ] Tests only

## Tests

List the checks you ran:

- [ ] `uv run --directory backend --locked ruff check --config pyproject.toml src ../integrations/mcp/src`
- [ ] `uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py"`
- [ ] `uv run --directory backend --locked python -m unittest discover -s tests/integration -p "*integration.py"`
- [ ] `uv run --directory backend --locked python -m unittest discover -s tests/migration -p "*.py"`
- [ ] `uv run --directory backend --locked python tests/import_boundaries.py`
- [ ] `uv run --directory integrations/mcp --locked python -m unittest discover -s tests -p "test_*.py"`
- [ ] `python -m unittest discover -s .github/scripts -p "test_*.py"` (release workflow changes; requires `packaging`)
- [ ] `npm run docs:build` (documentation changes)
- [ ] `npm --prefix web run typecheck && npm --prefix web run build`
- [ ] Manual smoke test:

## Notes

- User-facing behavior: linked English/Chinese guide or reference updates and Unreleased entry, or why no user documentation changes are needed.
- Test ownership: existing case extended, new boundary covered, or duplicate removed.
- Breaking changes:
- API-provider assumptions:
- Known limitations:

## Safety

- [ ] I did not commit API keys, tokens, cookies, private search history, or protected full-text content.
- [ ] This change does not add paywall bypass, credential sharing, or bulk protected PDF downloading.
