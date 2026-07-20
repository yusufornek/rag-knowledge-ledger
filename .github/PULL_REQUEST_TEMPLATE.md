## Summary

What does this change do and why?

## Related issue(s)

Closes #

## Changes

-

## Testing

Describe the tests you added or ran. Commands and their result:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Manifest / schema compatibility

- [ ] This change does not modify `docs/spec/manifest-v1.schema.json` or
      `docs/spec/policy-v1.schema.json`, or
- [ ] This change modifies a committed schema, and the rationale,
      updated fixtures, and a `CHANGELOG.md` entry are included.

## Checklist

- [ ] Tests were added or updated for the change.
- [ ] `CHANGELOG.md` was updated under `Unreleased`, if user-facing.
- [ ] Documentation was updated, if applicable.
- [ ] No secrets, credentials, or real personal data are included in
      this pull request (including test fixtures).
