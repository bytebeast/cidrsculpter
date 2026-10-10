# Changelog

## v1.1.0 (2026-10-10)

### Bug Fixes

- **terraform/aws**: unnamed subnets got labels like `"10_0_1_0_24"`, which
  Terraform rejects (labels cannot start with a digit); they are now
  `subnet_10_0_1_0_24`.
- **terraform (all)**: tag keys were written unquoted, so keys with spaces or
  `:` broke the file; values containing `${` or `%{` became interpolations. Keys
  are quoted and template sequences escaped.
- **terraform/aws IPv6**: subnets were given the plan's literal prefixes inside
  an Amazon-assigned /56 they could never match, with no IPv4 range, so apply
  failed. Subnets are now placed by offset with `cidrsubnet()` as IPv6-only
  (`ipv6_native`).
- **terraform/azure, terraform/gcp IPv6**: produced configurations the clouds
  reject (GCP `ip_cidr_range` is IPv4-only; Azure needs IPv4 on every subnet).
  These exports now refuse with an explanation.
- **terraform/azure names**: names could start with `_` (rejected by Azure), and
  reserved subnet names were lowercased (`gatewaysubnet`). Names now follow
  Azure's rules and keep `GatewaySubnet` etc. canonical.
- **cloud limits**: nothing stopped exporting blocks the cloud rejects (AWS /29
  subnets, a /8 VPC, Azure/GCP /30s, loopback or multicast ranges). Terraform
  exports now refuse an invalid plan and list what to fix.
- **host counts**: only the generic count (minus 2) was shown, overstating
  usable addresses by 3 on AWS/Azure and 2 on GCP.

### Features

- per-cloud usable addresses and a Cloud fit check in the detail panel; usable
  counts in the table, JSON and CSV exports
- AWS subnets get `availability_zone`: spread across `var.availability_zones`,
  or pinned with an `az` tag
- GCP subnetworks honour a `region` tag
- AWS JSON plan carries `valid_for_aws`, `errors`, `warnings` and per-subnet
  `usable_hosts`
- warnings for public ranges and the Docker bridge network on AWS
- test suite (`tests/test_cidrsculpter.py`, 55 tests)

## v1.0.1 (2026-08-07)

### Bug Fixes

- **cidrsculpter.py **: add author dunder (AH-2026080566777)
- **release**: cp'd check-endpoint, tried, i... (AH-2026080620360)

### Refactoring

- **release.py **: mv release.py from root to... (AH-2026080693279)

<!-- also in this release: ci -->

## v1.1.0 (2026-07-30)

### Features

- **cidrsculpter**: initial commit (AH-2026071373353)
- **cidrsclupter**: add cidrsculpter license (AH-2026071374620)
- **cidrsculpter.py**: comment cleanup (AH-2026071482042)
- **cidrsculpter**: add screenshot (AH-2026071628719)
- **cidrsculpter**: bug fixes (AH-2026071640131)
- **cidrsculpter **: add cidrsculpter social image (AH-2026071752571)
- **pyproj & workflows**: add workflows and pyproject (AH-2026073080749)
- **cidrsculpter.py**: add app version (AH-2026073086368)
- **cidrsculpter.py **: add requirements.txt (AH-2026073089415)
