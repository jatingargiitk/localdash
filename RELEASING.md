# Releasing localdash to PyPI

This project uses PyPI trusted publishing with GitHub Actions. No API tokens are needed.

## One-time setup (already completed)

To enable trusted publishing for this project, the following was configured at pypi.org:

1. Sign in to PyPI with 2FA enabled
2. Go to Account → Publishing → "Add a new pending publisher"
3. Enter the following details:
   - **PyPI project name**: `localdash`
   - **Owner**: `jatingargiitk`
   - **Repository name**: `localdash`
   - **Workflow filename**: `publish.yml`
   - **Environment name**: `pypi`

This configuration allows the GitHub Actions workflow to publish to PyPI without storing any credentials.

## Releasing a new version

1. Update the version in `pyproject.toml` and `localdash/__init__.py`
2. Commit and push: `git commit -am "Bump version to X.Y.Z" && git push`
3. Create a GitHub release:
   - Go to https://github.com/jatingargiitk/localdash/releases/new
   - Tag: `vX.Y.Z` (e.g., `v0.1.0`)
   - Title: `vX.Y.Z`
   - Description: Brief notes on what's new
   - Click "Publish release"
4. The workflow at `.github/workflows/publish.yml` will automatically:
   - Build the source distribution and wheel
   - Run `twine check` to validate them
   - Publish to PyPI using trusted publishing

Check the workflow run at https://github.com/jatingargiitk/localdash/actions

## Manual trigger

The workflow can also be triggered manually via the GitHub Actions UI using the "workflow_dispatch" option.

## Verifying the release

After publishing:
```bash
pip install --upgrade localdash
localdash --version
```
