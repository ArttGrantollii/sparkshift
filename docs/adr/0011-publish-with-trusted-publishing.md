# ADR 0011: publish to PyPI with Trusted Publishing, after TestPyPI and approval

- **Status:** Accepted
- **Date:** 2026-10-15

## Context

SparkShift is ready for its first public release. Publishing to PyPI is
permanent: a version number can never be uploaded again, even after it is
deleted. The traditional way to publish from CI is an API token stored as a
repository secret, which is a long-lived credential that can leak.

## Decision

- **Trusted Publishing.** The release workflow gets a short-lived OIDC token
  from GitHub, and PyPI accepts it because the project lists this repository,
  `release.yml`, and an environment as its publisher. No PyPI token exists.
- **Published only from a version tag**, after every CI job passes on the
  tagged commit (the CI workflow is reusable for this), and after checking
  that the tag matches the package version and the CHANGELOG has notes for it.
- **TestPyPI first**, then a clean installation from it, then PyPI. Only
  SparkShift is installed from TestPyPI; its dependencies come from PyPI, so
  a package uploaded to TestPyPI under a dependency's name cannot be used
  instead.
- **A person approves** the PyPI step: the `pypi` environment requires a
  reviewer. Pre-releases (`0.1.0rc1`) stop at TestPyPI, as a rehearsal.
- **The first release is 0.1.0**, not 1.0.0. Under semantic versioning, 1.0
  promises a stable API; SparkShift has not been used by anyone else yet,
  and its generated output was still changing. 1.0 can follow once it has.
- **The PyPI description is the README**, with its relative links rewritten
  to GitHub links at build time (`hatch-fancy-pypi-readme`), because PyPI
  cannot follow links into the repository. The package check rejects a
  description with relative links.

## Consequences

- Releasing needs a one-time setup outside the repository: PyPI and
  TestPyPI accounts with two-factor authentication, a trusted publisher on
  each, and the reviewer on the `pypi` environment.
- The publish action is pinned to an exact version, as a third-party action
  that handles the release.
- See [releasing.md](../releasing.md) for the steps.
