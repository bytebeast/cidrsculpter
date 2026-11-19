# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install pytest rich textual
python3 -m pytest -q tests                          # full suite
python3 -m pytest -q tests -k test_aws_ipv6         # single test / pattern
ruff check . && ruff format --check .               # what CI runs (ruff.yml)
./cidrsculpter.py                                   # run the TUI
```

Ruff config lives only in `pyproject.toml` (line-length 88, `E4/E7/E9 F W I UP B`). CI deliberately reads it from there, so add rules to `pyproject.toml`, never as CLI flags in a workflow.

## Architecture

Everything is one file, `cidrsculpter.py`, installed by copying it onto PATH. There is intentionally no package or `[build-system]`. Runtime deps are only `rich` and `textual`; all address math uses stdlib `ipaddress`. Keep it that way.

The file is layered top to bottom:

1. **`CIDRModel`**: a flat `dict[cidr -> Node]` where each node holds parent, children and tags. Split always produces exactly two halves, join collapses them. `to_dict`/`from_dict` is the save-file format (see README). Works identically for IPv4 and IPv6.
2. **Naming helpers** (`tf_identifier`, `cidr_label`, `hcl_escape`, `_azure_name`, `_gcp_name`, `UniqueNamer`, `filename_safe`): every renderer goes through these so labels are valid and unique. Unnamed blocks become `subnet_<cidr>` because Terraform labels cannot start with a digit.
3. **Cloud rules** (`block_issues`, `plan_issues`, `usable_hosts`): return `(errors, warnings)`. The root is checked as the VPC/VNet (not for GCP IPv4, which has no network range) and every leaf as a subnet. Errors mean the cloud would reject it; warnings get written as a comment header in the `.tf`.
4. **Renderers** (`render_aws_tf`, `render_azure_tf`, `render_gcp_tf`, `render_aws_plan`): pure functions of `(model, plan_name)`. The Terraform renderers raise `ExportBlocked` when `plan_issues` has errors. `render_aws_plan` never blocks, it embeds `valid_for_aws`/`errors`/`warnings` instead.
5. **Textual UI** (`Screen` subclasses and `CIDRSculpterApp`): `BINDINGS` maps keys to `action_*` methods. Cloud Terraform exports go through `_write_cloud_export`, which writes **no file** on `ExportBlocked` so a stale export is never mistaken for a fresh one. The non-cloud exports (JSON, CSV, DOT, text, Markdown, ADF, Confluence) are implemented inline as `action_export_*` methods on the app, built from `_export_base()`.

IPv6 cloud behaviour is a deliberate design choice, not a gap. AWS IPv6 subnets are emitted as `cidrsubnet(vpc.ipv6_cidr_block, newbits, netnum)` offsets inside Amazon's /56 with `ipv6_native = true`, because AWS picks the actual prefix. Azure and GCP refuse IPv6-only plans outright.

Tag semantics that affect output: `name` drives resource labels and display names, `az`/`availability_zone` pins an AWS subnet, `region` overrides the GCP subnetwork region. Azure keeps the `name` tag's case so reserved names like `GatewaySubnet` survive.

## Tests

`tests/test_cidrsculpter.py` loads the script via `importlib` (it must register the module in `sys.modules` for dataclasses to work). The last section drives the real app headless with Textual's `app.run_test()` pilot and writes exports into `tmp_path`.

## Releases

Versioning is automated from conventional commits by `.github/workflows/release.yml` and `.github/scripts/release.py`: `feat` bumps minor, `fix`/`perf` patch, `!` or `BREAKING CHANGE:` major, anything else no release. The version lives in three places kept in sync by the script: `APP_VERSION` in `cidrsculpter.py`, `version` in `pyproject.toml`, and the README badge. Do not bump these by hand. `main` is protected, so the workflow opens a `release/vX.Y.Z` PR and tags after it merges. The next version is bumped from the last tag, not the files. A version with no matching `## vX.Y.Z` CHANGELOG heading is left untagged on purpose. Release PRs are opened with a GitHub App token (`vars.RELEASE_APP_CLIENT_ID`, `secrets.RELEASE_APP_PRIVATE_KEY`) so their checks run. `release.py` and `tests/test_release.py` are kept in sync with bytebeast/check-endpoint; only `VERSION_FILES` differs.

Commit subjects take a ticket reference, e.g. `fix(terraform): quote tag keys (AH-123)`.
