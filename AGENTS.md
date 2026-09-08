# Development agreements

- Write all Git commit messages in English following Conventional Commits 1.0.0-beta.4
- Use `<type>(<scope>): <description>` or `<type>: <description>`
- Breaking changes require a `BREAKING CHANGE:` body or footer
- Validate commit messages before committing and do not rewrite existing history
- Never pass OAuth URLs, confirmation codes or credentials to model-facing interfaces
- Resolve identity from an authenticated host adapter, never tool arguments or prompts
- Keep integration limitations explicit and use isolated test environments
- Run `python3 -m unittest discover -s tests -v` before committing code changes
