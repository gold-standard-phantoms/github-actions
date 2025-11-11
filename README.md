# Reusable GitHub Actions and Workflows

This repository contains reusable GitHub Actions and Workflows designed to be shared across multiple repositories within the Gold Standard Phantoms organisation.

## Workflows

### `deploy-to-gsp-pypi.yml`

This workflow is designed to build and deploy a Python package to the Gold Standard Phantoms Private PyPI server.

**Usage:**

To use this workflow in your repository, create a workflow file (e.g., `.github/workflows/deploy.yml`) with the following content:

```yaml
name: Deploy to PyPI

on:
  push:
    branches:
      - main
  release:
    types: [created]

jobs:
  deploy_job:
    name: Deploy Package
    uses: gold-standard-phantoms/github-actions/.github/workflows/deploy-to-gsp-pypi.yml@main # Replace 'main' with a tag or commit SHA for production
    with:
      python_version: "3.11" # Replace with the version of Python you want to use for testing and linting
      # ruff_version: "0.9.10" # Optional: Override the version from uv.lock
      # mypy_version: "1.15.0" # Optional: Override the version from uv.lock
      # uv_lock_path: "./backend/uv.lock" # Optional: Custom path for monorepos
      # working_directory: "./backend" # Optional: Working directory for monorepos
      use_dvc: true # Whether to use DVC to pull data. Expects AWS credentials to be set in the repository secrets.
    secrets: inherit
```

**Secrets Required in Calling Repository:**

The calling repository (the repository where you use this workflow) must have the following secrets defined:

*   `PRIVATE_PYPI_USERNAME`:  Username for the private PyPI server.
*   `PRIVATE_PYPI_PASSWORD`: Password or token for the private PyPI server.
*   `PRIVATE_PYPI_URL`: URL of the private PyPI server.

**Tool Version Management:**

By default, the workflows will automatically use the versions of `ruff` and `mypy` specified in your project's `uv.lock` file. This ensures consistency between your local development environment and CI/CD. You can:

- Override tool versions by explicitly setting `ruff_version` or `mypy_version`
- Specify a custom `uv.lock` path for monorepo setups using `uv_lock_path`
- Set a custom working directory for monorepo projects using `working_directory`
- If no `uv.lock` is found or the tools aren't present in it, the workflows will use the latest versions

**Monorepo Support:**

For monorepo projects where your Python package is not in the repository root, use the `working_directory` parameter to specify the path to your Python project. This parameter is supported by both `deploy-to-gsp-pypi.yml` and `python-test.yml` workflows

**Workflow Details:**

The `deploy-to-gsp-pypi.yml` workflow internally performs the following steps:

1.  **Runs Tests:** Calls the `python-test.yml` reusable workflow to execute linting, commit checks, and tests.
2.  **Builds Package:** Builds the Python package using `pyproject-build`.
3.  **Deploys to PyPI:** Uploads the built package to the private PyPI server using `twine`.

### `python-test.yml`

This workflow is a reusable workflow that performs linting, commit checks, and runs Python tests.

**Usage:**

To use this workflow to run tests on push events in your repository (excluding the `main` branch, which is typically handled by the deployment workflow), create a workflow file (e.g., `.github/workflows/push-tests.yml`) with the following content:

```yaml
name: Run Tests on Push

on:
  push:
    branches:
      - "**"
      - "!main"

jobs:
  test_job:
    name: Run Push Tests
    uses: gold-standard-phantoms/github-actions/.github/workflows/python-test.yml@main # Replace 'main' with a tag or commit SHA for production
    with:
      python_version: "3.11" # Replace with the version of Python you want to use for testing and linting
      # ruff_version: "0.9.10" # Optional: Override the version from uv.lock
      # mypy_version: "1.15.0" # Optional: Override the version from uv.lock
      # uv_lock_path: "./backend/uv.lock" # Optional: Custom path for monorepos
      # working_directory: "./backend" # Optional: Working directory for monorepos
      use_dvc: true # Whether to use DVC to pull data. Expects AWS credentials to be set in the repository secrets.
      docs: false # Whether to evaluate mkdocs builds without error. 
    secrets: inherit
```

**Secrets Required in Calling Repository:**

The calling repository must have the following secret defined:

*   `PRIVATE_PYPI_PASSWORD`:  This secret is used to construct the PyPI index URL for testing against the private PyPI server.

**Workflow Details:**

The `python-test.yml` workflow internally performs the following jobs:

1.  **Lint Checks:** Runs linting checks using `ruff` and `mypy`.
2.  **Commit Checks:** Checks if commit messages follow the Conventional Commits specification.
3.  **Tests:** Runs Python tests using `tox`.

## Actions

This repository also contains reusable GitHub Actions that are used by the workflows and can be used independently if needed.  For detailed documentation on each action (inputs, outputs, and usage examples), please refer to the `action.yml` file within each action's directory.

*   `build-and-deploy-pypi`: Builds and deploys a Python package to PyPI.
*   `commit-check`: Checks commit messages against the Conventional Commits specification.
*   `lint`: Runs Python linting checks.
*   `setup-and-test`: Sets up a Python environment and runs tests using `tox`.
*   `setup-python`: Sets up a Python environment with `uv` and configures PyPI index URLs.
*   `static-analysis`: Performs static analysis checks (currently empty - for future expansion).

## Versioning and Stability

**Important:** When using these reusable workflows and actions in production, **always use tags or commit SHAs instead of branch names like `main`**.  Using branch names can lead to unpredictable behaviour if the `main` branch of this `github-actions` repository is updated with breaking changes.

**Example using tags:**

```yaml
uses: gold-standard-phantoms/github-actions/.github/workflows/deploy-to-gsp-pypi.yml@v1.0.0 # Using tag v1.0.0
```

We recommend creating tags for releases of this `github-actions` repository (e.g., `v1.0.0`, `v1.1`, `v2`) and using these tags in your workflow `uses:` statements to ensure stability and reproducibility.

---

For any questions or issues, please refer to the documentation for individual workflows and actions or contact the Gold Standard Phantoms Platform team.