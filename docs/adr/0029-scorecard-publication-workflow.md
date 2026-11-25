<!--
SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>

SPDX-License-Identifier: CC-BY-SA-4.0
-->

# ADR-0029: Publish OpenSSF Scorecard results from a dedicated workflow

- **Status**: Accepted
- **Date**: 2026-11-23
- **Deciders**: @lusoris
- **Tags**: `ci`, `security`, `supply-chain`

## Context

The main-branch scan at `a636bcd4` signed its results, but the OpenSSF service rejected publication because the signing job included the repository's draft-check action. The service inspects the workflow file named in the signing certificate. It checks the steps declared in that file even when an event condition skips them. [Failed run 45103638672](https://github.com/plurano/golusoris/actions/runs/45103638672) · [Scorecard workflow restrictions](https://github.com/ossf/scorecard-action/tree/fa7117db83c52d787ae5a89f350999701505d78e#workflow-restrictions)

## Decision

`scorecard-publish.yml` owns main-branch pushes and the Monday 06:15 UTC schedule. Its single job receives the OIDC signing permission and runs checkout, Scorecard and both SARIF uploads. `scorecard.yml` keeps the required `Scorecard Analysis` check, including the draft check on pull requests and the notice on merge-queue entries. Both workflows support manual and reusable invocation.

## Alternatives considered

Putting both Scorecard jobs in one file would make publication depend on which job the service selects during verification. Separate files give the verifier exactly one Scorecard job in the signing workflow.

## Consequences

Each push to main triggers one Scorecard scan and one code-scanning upload. The policy fixtures in `scripts/ci/actionlint-policy_test.sh` exercise both workflows and the required status context.

The downstream starter in `template/.github/workflows/ci.yml` still calls `scorecard.yml` at v0.22.2. It does not call the publication workflow. Downstream repositories need a separate publisher invocation to adopt this split.
