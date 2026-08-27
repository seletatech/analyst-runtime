# Analyst Runtime release wheelhouse

This directory is a release-time deployment cache for the Linux CPython 3.12
wheels locked by `../uv.lock`. Wheel binaries are intentionally ignored by
Git. A release operator must hydrate this directory before building the image;
`Dockerfile.mvp` verifies package hashes from the locked `uv export` output and
installs without network access. A missing or incomplete wheelhouse fails the
build instead of falling back to the network.

Do not treat this directory as source code or commit wheel binaries.
