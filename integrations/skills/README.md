# PaperSeek Agent Skill

`integrations/skills/paperseek/` is a thin usage guide for an installed PaperSeek v1.0.0-beta3 Community product. It does not contain a second search runtime and never stores API keys, cookies, or account credentials.

The Skill routes agents to the formal product surfaces:

- `paperseek search`, `serve`, `worker`, `doctor`, `migrate`, and `version`;
- the local `/api/v1` service exposed by `paperseek serve`;
- the separately installed, exactly matched `paperseek-mcp` distribution.

Install the Community package from this repository with:

```powershell
python -m pip install -e ./backend
paperseek doctor --json
```

Copying the Skill without installing PaperSeek does not provide a fallback search implementation. See `paperseek/SKILL.md` and its references for source choice, command contracts, and safe configuration guidance.
