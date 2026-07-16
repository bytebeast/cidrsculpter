# CIDRSculpter

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/Version-1.0.0-89b4fa?style=flat">
  <img alt="Status" src="https://img.shields.io/badge/Status-beta-f9e2af?style=flat">
  <a href="https://github.com/bytebeast/cidrsculpter/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-a6e3a1?style=flat"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python" src="https://img.shields.io/badge/Python-3.10+-cba6f7?style=flat"></a>
</p>


> An interactive terminal UI for planning and sculpting IP address spaces —
> split, join, tag, visualise, and export to AWS, Azure, GCP, Terraform,
> Confluence, and more. Supports both IPv4 and IPv6.

---

## What It Does

CIDRSculpter lets you take a starting CIDR block (e.g. `10.0.0.0/20` or
`2001:db8::/32`) and interactively carve it into subnets. Each block can be
split in half, joined back together, and tagged with metadata (name,
environment, tier, etc.). When your plan looks right, export it in whatever
format your team needs.

No CIDR math in your head. No spreadsheets. No leaving the terminal.

---

## Screenshot

<!-- Add screenshot here -->

---

## Installation

```bash
pip install rich textual
chmod +x cidrsculpter.py
./cidrsculpter.py
```

Requires Python 3.10+. No external CIDR library, all address arithmetic uses
Python's built-in `ipaddress` module.

---

## Layout

The app uses a **vertical split**: the CIDR tree occupies the top 60% of the
terminal and the detail panel sits below it, scrollable, making it easy to read
subnet information without losing context of the tree above.

---

## Quick Start

1. Run `./cidrsculpter.py`
2. Enter a plan name (e.g. `Production VPC`)
3. Enter a starting CIDR, IPv4 **or** IPv6 (e.g. `10.0.0.0/20` or
   `2001:db8::/32`)
4. Use the keyboard to split, join, and tag blocks
5. Press a number key to export

---

## Key Bindings

### Navigation & Editing

| Key | Action                                           |
| --- | ------------------------------------------------ |
| `s` | Split selected block into two equal halves       |
| `j` | Join selected block's two children back into one |
| `a` | Add / edit tags on the selected CIDR             |
| `t` | Open full table view of all CIDRs                |
| `q` | Quit                                             |

### Save & Load

| Key      | Action                                                   |
| -------- | -------------------------------------------------------- |
| `ctrl+s` | Save the current plan to `<Plan_Name>_plan.json`         |
| `ctrl+o` | Load a previously saved plan (also available at startup) |

Plans are saved as JSON and can be reloaded to continue working. The save file
preserves the full split tree, all tags, and the plan name.

### Exports

| Key | Format                         | Output file                 |
| --- | ------------------------------ | --------------------------- |
| `1` | JSON                           | `<name>_cidr.json`          |
| `2` | Terraform: AWS                 | `<name>_terraform.tf`       |
| `3` | AWS VPC plan JSON              | `<name>_aws_vpc_plan.json`  |
| `4` | CSV                            | `<name>_cidr.csv`           |
| `5` | Graphviz DOT diagram           | `<name>_cidr.dot`           |
| `6` | Plain text (space-delimited)   | `<name>_subnets.txt`        |
| `7` | Markdown table                 | `<name>_subnets.md`         |
| `8` | ADF: Atlassian Document Format | `<name>_subnets.adf.json`   |
| `9` | Terraform: Azure               | `<name>_azure.tf`           |
| `0` | Terraform: GCP                 | `<name>_gcp.tf`             |
| `c` | Confluence wiki markup         | `<name>_subnets.confluence` |

Output filenames are derived from your plan name:

- Spaces → underscores
- Dots, slashes, colons → hyphens
- Consecutive hyphens collapsed to one

Examples: `prod/vpc 1.0` → `prod-vpc_1-0`, `2001:db8::/32` → `2001-db8-32`

_Notes: Create cidr diagram using dot file._
```bash
dot -Tpng <name>_cidr.dot -o <name>_cidr.png
```

---

## IPv4 and IPv6

CIDRSculpter works identically for both address families.

|                     | IPv4               | IPv6                                         |
| ------------------- | ------------------ | -------------------------------------------- |
| Starting CIDR       | `10.0.0.0/20`      | `2001:db8::/32`                              |
| Smallest splittable | `/31`              | `/127`                                       |
| Smallest block      | `/32`              | `/128`                                       |
| Host count display  | integer            | `2^N` notation                               |
| AWS Terraform       | `cidr_block`       | `ipv6_cidr_block` + note                     |
| Azure Terraform     | `address_prefixes` | same (native IPv6 support)                   |
| GCP Terraform       | `ip_cidr_range`    | `ip_cidr_range` + `ipv6_access_type` comment |

**Host count display:** IPv6 subnets contain astronomically large address counts
(a `/64` has 2⁶⁴ ≈ 18.4 quintillion addresses). CIDRSculpter displays these as
`2^N` notation throughout the UI and all exports.

**AWS IPv6 note:** AWS VPCs require an IPv4 CIDR, IPv6 is assigned separately.
When exporting an IPv6 plan to Terraform/AWS, a placeholder IPv4 block and
`assign_generated_ipv6_cidr_block = true` are included with an explanatory
comment.

---

## Tagging

Press `a` on any CIDR to add tags as `key=value` pairs (comma-separated):

```
name=bastion001,private=false,tier=bastions
```

The `name` tag is special: used as the Terraform resource identifier, shown
distinctly in diagrams, and as the display name in tables and exports.

---

## Sample Outputs

### Plain text (space-delimited)

```
# plan_name: Production VPC
# root_cidr: 10.0.0.0/20
CIDR          START       END          HOSTS  PARENT        DEPTH  IS_LEAF  NAME     OTHER_TAGS
10.0.0.0/20   10.0.0.1    10.0.15.254  4094   ROOT          0      no       -        -
10.0.0.0/22   10.0.0.1    10.0.3.254   1022   10.0.0.0/21   2      yes      private  tier=app
10.0.4.0/22   10.0.4.1    10.0.7.254   1022   10.0.0.0/21   2      yes      public   tier=web
10.0.8.0/21   10.0.8.1    10.0.15.254  2046   10.0.0.0/20   1      yes      data     tier=datastore
```

```bash
# print CIDR and name for all leaf subnets
awk 'NR>3 && $7=="yes" {print $1, $8}' Production_VPC_subnets.txt
```

### IPv6 plain text (host counts as 2^N)

```
# plan_name: IPv6 Plan
# root_cidr: 2001:db8::/32
CIDR                  HOSTS  IS_LEAF  NAME
2001:db8::/32         2^96   no       -
2001:db8::/33         2^95   yes      -
2001:db8:8000::/33    2^95   yes      -
```

### Terraform: AWS (IPv4)

```hcl
resource "aws_vpc" "production_vpc" {
  cidr_block = "10.0.0.0/20"
}
resource "aws_subnet" "private" {
  vpc_id     = aws_vpc.production_vpc.id
  cidr_block = "10.0.0.0/22"
  tags       = { Name = "private", tier = "app" }
}
```

### Terraform: AWS (IPv6)

```hcl
# NOTE: AWS VPCs require an IPv4 cidr_block. For IPv6, configure
# assign_generated_ipv6_cidr_block or an IPv6 IPAM pool on the VPC.
resource "aws_vpc" "ipv6_plan" {
  cidr_block                       = "10.0.0.0/16"
  assign_generated_ipv6_cidr_block = true
}
resource "aws_subnet" "subnet" {
  vpc_id          = aws_vpc.ipv6_plan.id
  ipv6_cidr_block = "2001:db8::/33"
}
```

### Terraform: Azure

```hcl
resource "azurerm_virtual_network" "production_vpc" {
  name          = "production_vpc"
  address_space = ["10.0.0.0/20"]
  location      = var.location
  resource_group_name = var.resource_group_name
}
resource "azurerm_subnet" "private" {
  name                 = "private"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.production_vpc.name
  address_prefixes     = ["10.0.0.0/22"]
}
```

### Terraform: GCP

```hcl
resource "google_compute_network" "production_vpc" {
  name                    = "production_vpc"
  project                 = var.project_id
  auto_create_subnetworks = false
}
resource "google_compute_subnetwork" "private" {
  name          = "private"
  project       = var.project_id
  region        = var.region
  ip_cidr_range = "10.0.0.0/22"
  network       = google_compute_network.production_vpc.id
  labels        = { name = "private", tier = "app" }
}
```

### Confluence wiki markup

```
h1. Production VPC

*Root CIDR:* +10.0.0.0/20+

||CIDR||Name||Start||End||Hosts||Leaf||Tags||
|{{monospace}}10.0.0.0/22{{/monospace}}|private|10.0.0.1|10.0.3.254|1022|(/)|tier=app|
|{{monospace}}10.0.4.0/22{{/monospace}}|public|10.0.4.1|10.0.7.254|1022|(/)|tier=web|
|{{monospace}}10.0.8.0/21{{/monospace}}|data|10.0.8.1|10.0.15.254|2046|(/)|tier=datastore|
```

Paste via **Edit → Insert → Markup → Confluence Wiki**.

---

## Save File Format

```json
{
  "plan_name": "Production VPC",
  "root_cidr": "10.0.0.0/20",
  "nodes": {
    "10.0.0.0/20": {
      "parent": null,
      "children": ["10.0.0.0/21", "10.0.8.0/21"],
      "tags": {}
    },
    "10.0.0.0/21": {
      "parent": "10.0.0.0/20",
      "children": ["10.0.0.0/22", "10.0.4.0/22"],
      "tags": {}
    },
    "10.0.0.0/22": {
      "parent": "10.0.0.0/21",
      "children": [],
      "tags": { "name": "private", "tier": "app" }
    }
  }
}
```

---

## Design Notes

- **No external CIDR library**: all arithmetic uses Python's built-in
  `ipaddress` module.
- **IPv4 and IPv6**: both address families are fully supported throughout the
  model, UI, and all eleven export formats.
- **IPv6 host counts as `2^N`**: a /64 contains 2⁶⁴ addresses; raw integers
  would be 20 digits long.
- **Vertical layout**: tree on top, scrollable detail panel below; easier to
  read on standard terminals.
- **Catppuccin Mocha theme**: throughout the TUI and Graphviz output.
- **All exports include root CIDR and plan name**: every format carries the
  starting CIDR so exports are self-contained.
- **Terraform resource names**: come from the `name` tag; the sanitised CIDR is
  used as fallback.
- **Azure**: `azurerm_subnet` has no `tags` block, subnet tags are emitted as
  comments.
- **GCP**: uses `labels` instead of `tags`; keys/values auto-sanitised to GCP
  constraints.
- **AWS IPv6:** VPCs require IPv4, exported Terraform includes a placeholder
  with an explanatory comment.

---

## Last Note

If CIDRSculpter saved you from a CIDR spreadsheet, a ⭐ is appreciated.

---

## License

MIT
