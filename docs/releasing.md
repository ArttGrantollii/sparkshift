# Releasing

A release is published by pushing a version tag. The
[release workflow](../.github/workflows/release.yml) then:

1. runs every CI job on the tagged commit;
2. checks that the tag is `v` followed by the version in
   `src/sparkshift/__init__.py` and, for a final release, that `CHANGELOG.md`
   has a section for it ([tools/release.py](../tools/release.py));
3. builds the source distribution and wheel and checks them: SQLGlot is the
   only required dependency, the Databricks SDK the only optional one, the
   description has no links PyPI cannot follow, and it renders on PyPI;
4. publishes to [TestPyPI](https://test.pypi.org/project/sparkshift/), installs
   that package in a clean environment, and runs it;
5. for a final release, waits for a maintainer to approve the `pypi`
   environment, publishes to [PyPI](https://pypi.org/project/sparkshift/), and
   creates a GitHub release with the CHANGELOG notes and the built files.

A pre-release, such as `0.1.0rc1`, stops after TestPyPI: use one to rehearse.

## Steps

1. Set `__version__` in `src/sparkshift/__init__.py` (for example `0.1.0`).
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.1.0] - YYYY-MM-DD`
   and add a new empty `## [Unreleased]` above it.
3. Commit (`chore(release): 0.1.0`), push, and wait for CI.
4. Tag and push the tag:

   ```bash
   git tag -a v0.1.0 -m "SparkShift 0.1.0"
   git push origin v0.1.0
   ```

5. When the workflow reaches **Publish to PyPI**, review and approve it.
6. Afterwards, set the version to the next development version (such as
   `0.2.0.dev0`).

A version can be published to PyPI only once, even if it is deleted later; a
mistake needs a new version number.

## Trusted Publishing

The workflow publishes with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): GitHub gives
the job a short-lived identity token, and PyPI accepts it because the project
names this repository, `release.yml`, and the environment as its publisher.
There is no PyPI token in the repository, in GitHub secrets, or anywhere
else. Each index is set up once, as a publisher with these values:

| Field | TestPyPI | PyPI |
|---|---|---|
| PyPI project name | `sparkshift` | `sparkshift` |
| Owner | `ArttGrantollii` | `ArttGrantollii` |
| Repository name | `sparkshift` | `sparkshift` |
| Workflow name | `release.yml` | `release.yml` |
| Environment name | `testpypi` | `pypi` |

The GitHub environment `pypi` requires a reviewer's approval, so nothing
reaches PyPI without a person deciding it should. See
[ADR 0011](adr/0011-publish-with-trusted-publishing.md).
