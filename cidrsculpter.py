#!/usr/bin/env python3
#
# CIDRSculpter: interactive CIDR planner with multi-cloud export
#
# Requirements:
#   pip install rich textual
#
# Key bindings (inside the app):
#   s        split selected block into two halves
#   j        join selected block's two children back into one
#   t        view full table of all CIDRs
#   a        add / edit tags on selected CIDR
#   ctrl+s   save plan to <Plan_Name>_plan.json in the working directory
#   ctrl+o   load an existing plan from a saved .json file
#   q        quit
#
# Exports:
#   1   JSON             (includes root CIDR + plan name)
#   2   Terraform        (AWS VPC + subnets)
#   3   AWS JSON plan    (vpc + subnets)
#   4   CSV              (# metadata header with root CIDR)
#   5   Graphviz DOT     (nested cluster diagram)
#   6   Plain text       (space-delimited, underscore headers, parseable with awk/cut)
#   7   Markdown         (table + root CIDR heading, ready to paste into docs)
#   8   ADF              (Atlassian Document Format: paste into Confluence / Jira)
#   9   Azure TF         (azurerm_virtual_network + azurerm_subnet)
#   0   GCP TF           (google_compute_network + google_compute_subnetwork)
#   c   Confluence wiki  (classic wiki-markup table)

from __future__ import annotations

import csv
import html
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static, Tree

__author__ = "github.com/bytebeast"
APP_VERSION = "1.1.0"

# =========================================================
# CATPPUCCIN THEME
# =========================================================

CATPPUCCIN = {
    "bg": "#1e1e2e",
    "fg": "#cdd6f4",
    "blue": "#89b4fa",
    "green": "#a6e3a1",
    "surface": "#313244",
    "surface1": "#45475a",
    "surface2": "#585b70",
    "mantle": "#181825",
    "crust": "#11111b",
    "yellow": "#f9e2af",
    "red": "#f38ba8",
    "pink": "#f5c2e7",
    "lavender": "#b4befe",
    "subtext": "#a6adc8",
    "overlay": "#6c7086",
}

CONTAINER_SHADE_CYCLE = [
    CATPPUCCIN["mantle"],
    CATPPUCCIN["crust"],
    CATPPUCCIN["surface1"],
    CATPPUCCIN["surface2"],
]


##=========================================================
## MODEL
##=========================================================


@dataclass
class Node:
    cidr: str
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_leaf(self) -> bool:
        return not self.children


class CIDRModel:
    def __init__(self, root: str):
        self.nodes: dict[str, Node] = {}
        self.root = root
        self.add(root, None)

    def add(self, cidr: str, parent: str | None):
        if cidr in self.nodes:
            return
        self.nodes[cidr] = Node(cidr=cidr, parent=parent)
        if parent:
            self.nodes[parent].children.append(cidr)

    def split(self, cidr: str) -> bool:
        node = self.nodes.get(cidr)
        if not node or not node.is_leaf:
            return False
        net = ipaddress.ip_network(cidr, strict=False)
        max_prefix = 128 if isinstance(net, ipaddress.IPv6Network) else 32
        if net.prefixlen >= max_prefix:
            return False
        a, b = map(str, net.subnets(prefixlen_diff=1))
        self.add(a, cidr)
        self.add(b, cidr)
        return True

    def join(self, cidr: str) -> bool:
        node = self.nodes.get(cidr)
        if not node or node.is_leaf:
            return False
        for child in node.children:
            child_node = self.nodes.get(child)
            if child_node and not child_node.is_leaf:
                return False
        for child in node.children:
            self.nodes.pop(child, None)
        node.children = []
        return True

    @staticmethod
    def _host_range(net) -> tuple[str, str, int]:
        if net.num_addresses == 1:
            addr = net.network_address
            return str(addr), str(addr), 1
        # IPv6 has no broadcast: all addresses are usable
        if isinstance(net, ipaddress.IPv6Network):
            return (
                str(net.network_address),
                str(net[-1]),
                net.num_addresses,
            )
        if net.num_addresses == 2:
            return str(net.network_address), str(net.broadcast_address), 2
        # IPv4: subtract network + broadcast addresses
        first = net.network_address + 1
        last = net.broadcast_address - 1
        return str(first), str(last), net.num_addresses - 2

    def info(self, cidr: str) -> dict:
        net = ipaddress.ip_network(cidr, strict=False)
        start, end, host_count = self._host_range(net)
        return {
            "cidr": cidr,
            "start": start,
            "end": end,
            "hosts": host_count,
            # Per-cloud usable addresses (IPv4 only): each cloud reserves some
            # addresses in every subnet (AWS/Azure 5, GCP 4), so the generic
            # count above overstates what you can actually assign.
            "usable": (
                None
                if isinstance(net, ipaddress.IPv6Network)
                else {c: usable_hosts(cidr, c) for c in CLOUDS}
            ),
            "tags": dict(self.nodes[cidr].tags),
        }

    def parent(self, cidr: str) -> str:
        return self.nodes[cidr].parent or "ROOT"

    def depth(self, cidr: str) -> int:
        d, n = 0, self.nodes.get(cidr)
        while n and n.parent:
            d += 1
            n = self.nodes.get(n.parent)
        return d

    def is_leaf(self, cidr: str) -> bool:
        node = self.nodes.get(cidr)
        return bool(node and node.is_leaf)

    def leaf_cidrs(self) -> list[str]:
        return sorted(
            (c for c, n in self.nodes.items() if n.is_leaf),
            key=lambda c: ipaddress.ip_network(c),
        )

    ##=========================================================
    ## Serialisation
    ##=========================================================

    def to_dict(self, plan_name: str) -> dict:
        """Serialise the full model to a JSON-safe dict for save files."""
        return {
            "plan_name": plan_name,
            "root_cidr": self.root,
            "nodes": {
                cidr: {
                    "parent": node.parent,
                    "children": node.children,
                    "tags": node.tags,
                }
                for cidr, node in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> CIDRModel:
        """Reconstruct a CIDRModel from a save-file dict."""
        root = data["root_cidr"]
        model = cls.__new__(cls)
        model.root = root
        model.nodes = {}
        for cidr, nd in data["nodes"].items():
            model.nodes[cidr] = Node(
                cidr=cidr,
                parent=nd.get("parent"),
                children=list(nd.get("children", [])),
                tags=dict(nd.get("tags", {})),
            )
        return model

    ##=========================================================
    ## Tags
    ##=========================================================
    def set_tags(self, cidr: str, tags: dict[str, str]):
        if cidr in self.nodes:
            self.nodes[cidr].tags = dict(tags)

    def get_tags(self, cidr: str) -> dict[str, str]:
        return dict(self.nodes[cidr].tags) if cidr in self.nodes else {}

    @staticmethod
    def parse_tag_string(raw: str) -> dict[str, str]:
        tags: dict[str, str] = {}
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip()
            if key:
                tags[key] = value.strip()
        return tags

    @staticmethod
    def tags_to_string(tags: dict[str, str]) -> str:
        return ",".join(f"{k}={v}" for k, v in tags.items())

    @staticmethod
    def split_name_tag(tags: dict[str, str]) -> tuple[str | None, dict[str, str]]:
        name: str | None = None
        others: dict[str, str] = {}
        for k, v in tags.items():
            if k.lower() == "name" and name is None:
                name = v
            else:
                others[k] = v
        return name, others


##=========================================================
## TERRAFORM / HCL HELPERS
##=========================================================


def tf_identifier(raw: str, fallback: str) -> str:
    """A valid Terraform resource label: letters, digits, '_' and '-', not
    starting with a digit. `fallback` is used as-is when `raw` sanitizes to
    nothing, so it must already be valid."""
    s = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return fallback
    if s[0].isdigit():
        s = f"_{s}"
    return s


def cidr_label(prefix: str, cidr: str) -> str:
    """Default resource label for an unnamed block, e.g. subnet_10_0_1_0_24.
    Terraform labels must not start with a digit, so a prefix is required."""
    return prefix + "_" + re.sub(r"[^0-9a-zA-Z]+", "_", cidr).strip("_").lower()


def hcl_escape(value: str) -> str:
    """Escape a value for a double-quoted HCL string. Besides quotes and
    backslashes this neutralizes template sequences, so a tag value of
    '${var.x}' stays literal text instead of becoming an interpolation."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return s.replace("${", "$${").replace("%{", "%%{")


def hcl_tags_block(tags: dict[str, str], indent: str = "  ") -> list[str]:
    """tags = { "key" = "value" }. Keys are always quoted: unquoted keys must
    be HCL identifiers, so 'team name' or 'app:tier' broke the file."""
    if not tags:
        return []
    keys = {k: f'"{hcl_escape(k)}"' for k in tags}
    width = max(len(q) for q in keys.values())
    lines = [f"{indent}tags = {{"]
    for k, v in tags.items():
        lines.append(f'{indent}  {keys[k].ljust(width)} = "{hcl_escape(v)}"')
    lines.append(f"{indent}}}")
    return lines


def _azure_name(s: str, fallback: str, max_len: int = 80) -> str:
    """Sanitize to an Azure VNet/subnet name: letters, digits, '_', '.', '-';
    must start with a letter or digit and end with a letter, digit or '_'.
    Case is preserved, which matters for the reserved subnet names
    (GatewaySubnet, AzureFirewallSubnet, AzureBastionSubnet, ...)."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", s.strip())
    s = re.sub(r"^[^A-Za-z0-9]+", "", s)[:max_len]
    s = re.sub(r"[.-]+$", "", s)
    return s or fallback


def _gcp_name(s: str, fallback: str = "network") -> str:
    """Sanitize a string into a valid GCP compute resource name.

    GCP compute network/subnetwork names must match RFC1035:
    [a-z]([-a-z0-9]*[a-z0-9])?  which means a leading lowercase letter,
    then lowercase letters, digits, or hyphens, not ending in a hyphen,
    1 to 63 characters. tf_identifier() produces underscores (fine for a
    Terraform resource label) which are not valid in a GCP name, so this
    is applied to the 'name' argument value specifically.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s or not s[0].isalpha():
        s = "n-" + s
    s = s[:63].strip("-")
    return s or fallback


def _tag_comment_block(tags: dict[str, str], indent: str = "  ") -> list[str]:
    """Render tags as reference-only comments.

    Used for resources that have no labels/tags argument (GCP compute
    networks and subnetworks), so the tags stay visible without emitting
    an unsupported block that would fail 'terraform validate'.
    """
    if not tags:
        return []
    lines = [
        f"{indent}# Labels/tags are not supported on this resource; "
        f"values shown for reference:"
    ]
    for k, v in tags.items():
        lines.append(f'{indent}# {k} = "{hcl_escape(v)}"')
    return lines


class UniqueNamer:
    def __init__(self):
        self._used: set[str] = set()

    def claim(self, base: str) -> str:
        candidate, i = base, 2
        while candidate in self._used:
            candidate = f"{base}_{i}"
            i += 1
        self._used.add(candidate)
        return candidate


def _is_ipv6(cidr: str) -> bool:
    """Return True if cidr is an IPv6 network."""
    try:
        return isinstance(
            ipaddress.ip_network(cidr, strict=False), ipaddress.IPv6Network
        )
    except ValueError:
        return False


def _fmt_hosts(n: int) -> str:
    """Format host count compactly. IPv6 counts are powers of 2 so we
    express them as 2^N rather than a 39-digit integer. The power-of-two
    test is done with exact integer arithmetic (n & (n - 1)) so values
    that are merely close to a power of two, such as an IPv4 /0 host count
    of 2^32 - 2, are not mislabeled as 2^N."""
    if n <= 10_000_000:
        return str(n)
    if n > 0 and (n & (n - 1)) == 0:  # exact power of two
        return f"2^{n.bit_length() - 1}"
    if n >= 10**18:
        return f"{n:.2e}"
    return str(n)


def filename_safe(s: str) -> str:
    s = re.sub(r"\s+", "_", s.strip())  # spaces  → underscores
    s = re.sub(r"\.+", "-", s)  # dots    → hyphens
    s = re.sub(r"/+", "-", s)  # slashes → hyphens
    s = re.sub(r":+", "-", s)  # colons  → hyphens (IPv6)
    s = re.sub(r"-{2,}", "-", s)  # collapse consecutive hyphens
    return re.sub(r"[^A-Za-z0-9_\-]", "", s) or "plan"


##=========================================================
## CLOUD RULES
##=========================================================
##
## Limits the providers enforce at create time. Checking them here means an
## export either applies cleanly or refuses with the reason, instead of
## producing Terraform that fails at `apply`.
##
## Sources (provider docs, current as of 2026):
##   AWS:   VPC and subnet IPv4 sizes /16-/28, 5 reserved addresses per
##          subnet; Amazon-provided IPv6 VPC blocks are /56, subnet IPv6
##          sizes /44-/64 in steps of 4.
##   Azure: smallest subnet /29, 5 reserved addresses per subnet;
##          IPv6 subnets are /64 and need an IPv4 range alongside.
##   GCP:   smallest subnet primary range /29, 4 reserved addresses;
##          IPv6 ranges are assigned by Google, not chosen.

CLOUDS = ("aws", "azure", "gcp")
CLOUD_LABEL = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}
CLOUD_RESERVED = {"aws": 5, "azure": 5, "gcp": 4}

# Ranges no cloud will accept for a VPC/VNet/subnet.
_FORBIDDEN_V4 = [
    (ipaddress.ip_network("127.0.0.0/8"), "loopback"),
    (ipaddress.ip_network("169.254.0.0/16"), "link-local"),
    (ipaddress.ip_network("224.0.0.0/4"), "multicast"),
    (ipaddress.ip_network("255.255.255.255/32"), "broadcast"),
]
_AZURE_FORBIDDEN_V4 = [
    (ipaddress.ip_network("168.63.129.16/32"), "Azure platform (168.63.129.16)"),
]
_PRIVATE_V4 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT, accepted by all three
]
_DOCKER_BRIDGE = ipaddress.ip_network("172.17.0.0/16")

# Azure subnets with reserved names and the largest prefix length each allows.
_AZURE_SPECIAL_SUBNETS = {
    "azurebastionsubnet": ("AzureBastionSubnet", 26),
    "azurefirewallsubnet": ("AzureFirewallSubnet", 26),
    "azurefirewallmanagementsubnet": ("AzureFirewallManagementSubnet", 26),
    "gatewaysubnet": ("GatewaySubnet", 29),
    "routeserversubnet": ("RouteServerSubnet", 27),
}


def usable_hosts(cidr: str, cloud: str | None = None) -> int:
    """Addresses you can assign to hosts. Without a cloud: IPv4 minus network
    and broadcast (all for /31 and /32), IPv6 all. With a cloud: minus that
    cloud's reserved addresses per subnet."""
    net = ipaddress.ip_network(cidr, strict=False)
    if cloud is None:
        return CIDRModel._host_range(net)[2]
    return max(net.num_addresses - CLOUD_RESERVED[cloud], 0)


def block_issues(
    cidr: str,
    cloud: str,
    role: str,
    name: str | None = None,
    check_public: bool = True,
):
    """Problems with one block for one cloud. role is 'vpc' or 'subnet'.
    Returns (errors, warnings): errors mean the cloud will reject it.
    check_public=False skips the not-private-space warning (plan_issues only
    asks for it once, on the first block, since subnets inherit it)."""
    net = ipaddress.ip_network(cidr, strict=False)
    v6 = isinstance(net, ipaddress.IPv6Network)
    p = net.prefixlen
    errors: list[str] = []
    warnings: list[str] = []
    what = (
        {"aws": "VPC", "azure": "VNet", "gcp": "network"}[cloud]
        if role == "vpc"
        else "subnet"
    )
    label = f"{CLOUD_LABEL[cloud]} {what} {cidr}"

    if v6:
        if cloud == "gcp":
            errors.append(
                f"{label}: GCP assigns IPv6 ranges itself (ULA or external); "
                "they cannot be chosen. Plan the IPv4 ranges and enable "
                "stack_type = IPV4_IPV6 on the subnets instead."
            )
        elif cloud == "azure":
            errors.append(
                f"{label}: Azure has no IPv6-only VNets or subnets; every subnet "
                "also needs an IPv4 range. Plan the IPv4 space and add the IPv6 "
                "prefixes to it."
            )
        elif role == "vpc" and p != 56:
            errors.append(
                f"{label}: Amazon-provided IPv6 blocks are /56, so the plan must "
                "start from a /56 (only the offsets are used; AWS picks the "
                "addresses). For other sizes use an IPv6 IPAM pool."
            )
        elif role == "subnet" and not (44 <= p <= 64 and p % 4 == 0):
            errors.append(
                f"{label}: AWS IPv6 subnets must be /44 to /64 in steps of 4 "
                "(/56, /60 or /64 inside a /56 VPC)."
            )
        return errors, warnings

    for forbidden, why in _FORBIDDEN_V4 + (
        _AZURE_FORBIDDEN_V4 if cloud == "azure" else []
    ):
        if net.overlaps(forbidden):
            errors.append(f"{label}: overlaps the {why} range {forbidden}.")

    if cloud == "aws":
        if p < 16:
            errors.append(f"{label}: AWS allows /16 at most (this is /{p}).")
        if p > 28:
            errors.append(f"{label}: AWS allows /28 at least (this is /{p}).")
        if net.overlaps(_DOCKER_BRIDGE):
            warnings.append(
                f"{label}: overlaps 172.17.0.0/16, the Docker bridge default; "
                "some AWS services (SageMaker, Cloud9, ECS on EC2) conflict with it."
            )
    elif cloud == "azure":
        if p > 29:
            errors.append(f"{label}: Azure allows /29 at least (this is /{p}).")
        if role == "subnet" and name:
            special = _AZURE_SPECIAL_SUBNETS.get(name.lower())
            if special and p > special[1]:
                errors.append(
                    f"{label}: {special[0]} must be /{special[1]} or larger "
                    f"(this is /{p})."
                )
    elif cloud == "gcp" and role == "subnet":
        if p > 29:
            errors.append(f"{label}: GCP allows /29 at least (this is /{p}).")

    if check_public and not any(net.subnet_of(r) for r in _PRIVATE_V4):
        warnings.append(
            f"{label}: not in RFC 1918 or 100.64.0.0/10 private space. Only use "
            "public addresses you own (BYOIP), or instances will not reach "
            "whoever really owns them."
        )
    return errors, warnings


def plan_issues(model: CIDRModel, cloud: str):
    """(errors, warnings) for exporting the whole plan to `cloud`: the root is
    checked as the VPC/VNet (GCP networks have no range of their own) and
    every leaf as a subnet. Repeated warnings are reported once."""
    errors: list[str] = []
    warnings: list[str] = []
    checks = []
    if cloud != "gcp" or _is_ipv6(model.root):
        checks.append((model.root, "vpc"))
    checks += [(c, "subnet") for c in model.leaf_cidrs() if c != model.root]
    for index, (cidr, role) in enumerate(checks):
        name, _ = CIDRModel.split_name_tag(model.get_tags(cidr))
        e, w = block_issues(cidr, cloud, role, name, check_public=index == 0)
        errors += e
        warnings += [x for x in w if x not in warnings]
    if _is_ipv6(model.root) and cloud != "aws":
        errors = errors[:1]  # one explanation is enough
    return errors, warnings


class ExportBlocked(Exception):
    """Raised by a render_* function when the cloud would reject the plan."""

    def __init__(self, cloud: str, errors: list[str]):
        super().__init__("; ".join(errors))
        self.cloud = cloud
        self.errors = errors


def _issue_header(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    lines = ["#", "# WARNINGS (the cloud will accept these, but check them):"]
    for w in warnings:
        lines.append(f"#   - {w}")
    return lines


def _tag_lookup(tags: dict[str, str], *keys: str) -> str | None:
    for k, v in tags.items():
        if k.lower() in keys and v.strip():
            return v.strip()
    return None


def _tf_tags_for(model: CIDRModel, cidr: str) -> dict[str, str]:
    name_tag, other = CIDRModel.split_name_tag(model.get_tags(cidr))
    result: dict[str, str] = {}
    if name_tag:
        result["Name"] = name_tag
    result.update(other)
    return result


def _res_label(namer: UniqueNamer, model: CIDRModel, cidr: str, fallback: str):
    """(unique Terraform label, came-from-name-tag)."""
    name_tag, _ = CIDRModel.split_name_tag(model.get_tags(cidr))
    if name_tag:
        return namer.claim(tf_identifier(name_tag, fallback)), True
    return namer.claim(fallback), False


def render_aws_tf(model: CIDRModel, plan_name: str) -> str:
    """Terraform for an AWS VPC and its subnets.

    Subnets are spread over var.availability_zones in address order; a subnet
    tagged az=<zone> (or availability_zone=<zone>) is pinned to that zone.
    For an IPv6 plan the VPC gets an Amazon-provided /56 and each subnet is
    placed at the same offset inside it with cidrsubnet(), as IPv6-only.
    """
    errors, warnings = plan_issues(model, "aws")
    if errors:
        raise ExportBlocked("aws", errors)

    namer = UniqueNamer()
    is_v6 = _is_ipv6(model.root)
    root_net = ipaddress.ip_network(model.root, strict=False)
    vpc, vpc_named = _res_label(
        namer, model, model.root, tf_identifier(plan_name, "main")
    )
    subnets = [c for c in model.leaf_cidrs() if c != model.root]
    needs_az_var = any(
        _tag_lookup(model.get_tags(c), "az", "availability_zone") is None
        for c in subnets
    )

    lines = [
        "# CIDRSculpter: Terraform / AWS",
        f"# Plan:      {plan_name}",
        f"# Root CIDR: {model.root}",
    ]
    if is_v6:
        lines += [
            "#",
            "# IPv6: AWS assigns the VPC's /56, so the addresses below are placed",
            "# by OFFSET inside it with cidrsubnet(); the plan's layout is kept",
            "# but the actual prefixes are Amazon's. Subnets are IPv6-only.",
            "# A VPC always needs an IPv4 block too (var.vpc_ipv4_cidr).",
        ]
    lines += _issue_header(warnings)
    lines.append("")

    if needs_az_var:
        lines += [
            'variable "availability_zones" {',
            '  description = "AZs to spread subnets across in address order, '
            'e.g. [\\"us-east-1a\\", \\"us-east-1b\\"]. '
            'A subnet tagged az=<zone> uses that zone instead."',
            "  type        = list(string)",
            "}",
            "",
        ]
    if is_v6:
        lines += [
            'variable "vpc_ipv4_cidr" {',
            '  description = "IPv4 block AWS requires on every VPC; pick one '
            'that does not overlap your other networks."',
            "  type        = string",
            '  default     = "10.0.0.0/16"',
            "}",
            "",
        ]

    lines.append(f'resource "aws_vpc" "{vpc}" {{')
    if vpc_named:
        lines.append("  # resource name from 'name' tag")
    if is_v6:
        lines += [
            "  cidr_block                       = var.vpc_ipv4_cidr",
            "  assign_generated_ipv6_cidr_block = true",
        ]
    else:
        lines.append(f'  cidr_block = "{model.root}"')
    lines += hcl_tags_block(_tf_tags_for(model, model.root))
    lines.append("}")

    az_index = 0
    for c in subnets:
        label, named = _res_label(namer, model, c, cidr_label("subnet", c))
        az = _tag_lookup(model.get_tags(c), "az", "availability_zone")
        if az:
            az_expr = f'"{hcl_escape(az)}"'
        else:
            az_expr = f"element(var.availability_zones, {az_index})"
            az_index += 1

        lines += ["", f'resource "aws_subnet" "{label}" {{']
        if named:
            lines.append("  # resource name from 'name' tag")
        if is_v6:
            net = ipaddress.ip_network(c, strict=False)
            newbits = net.prefixlen - root_net.prefixlen
            netnum = (int(net.network_address) - int(root_net.network_address)) >> (
                128 - net.prefixlen
            )
            lines += [
                f"  # plan: {c}",
                f"  vpc_id                                         = aws_vpc.{vpc}.id",
                f"  ipv6_cidr_block                                = cidrsubnet(aws_vpc.{vpc}.ipv6_cidr_block, {newbits}, {netnum})",
                "  ipv6_native                                    = true",
                "  assign_ipv6_address_on_creation                = true",
                "  enable_resource_name_dns_aaaa_record_on_launch = true",
                '  private_dns_hostname_type_on_launch            = "resource-name"',
                f"  availability_zone                              = {az_expr}",
            ]
        else:
            lines += [
                f"  vpc_id            = aws_vpc.{vpc}.id",
                f'  cidr_block        = "{c}"',
                f"  availability_zone = {az_expr}",
            ]
        lines += hcl_tags_block(_tf_tags_for(model, c))
        lines.append("}")
    return "\n".join(lines) + "\n"


def render_aws_plan(model: CIDRModel, plan_name: str) -> dict:
    """The AWS JSON plan, with per-subnet usable counts and any issues."""
    errors, warnings = plan_issues(model, "aws")
    return {
        "plan_name": plan_name,
        "root_cidr": model.root,
        "vpc": model.root,
        "valid_for_aws": not errors,
        "errors": errors,
        "warnings": warnings,
        "subnets": [
            {
                "cidr": c,
                "usable_hosts": usable_hosts(c, "aws"),
                "tags": model.get_tags(c),
            }
            for c in model.leaf_cidrs()
            if c != model.root
        ],
    }


def render_azure_tf(model: CIDRModel, plan_name: str) -> str:
    """Terraform for an Azure VNet and its subnets (IPv4).

    The Azure `name` keeps the name tag's case, so reserved subnet names such
    as GatewaySubnet work; unnamed blocks get vnet-/snet- names that Azure
    accepts. azurerm_subnet has no tags argument, so subnet tags become
    comments.
    """
    errors, warnings = plan_issues(model, "azure")
    if errors:
        raise ExportBlocked("azure", errors)

    namer = UniqueNamer()
    vnet_tag, _ = CIDRModel.split_name_tag(model.get_tags(model.root))
    vnet_res = namer.claim(tf_identifier(vnet_tag or plan_name, "main"))
    vnet_name = _azure_name(
        vnet_tag or plan_name, "vnet-" + cidr_label("", model.root)[1:], 64
    )

    lines = [
        "# CIDRSculpter: Terraform / Azure",
        f"# Plan:      {plan_name}",
        f"# Root CIDR: {model.root}",
    ]
    lines += _issue_header(warnings)
    lines += [
        "",
        'variable "location" {',
        '  description = "Azure region"',
        "  type        = string",
        '  default     = "eastus"',
        "}",
        "",
        'variable "resource_group_name" {',
        '  description = "Name of an existing resource group"',
        "  type        = string",
        "}",
        "",
        f'resource "azurerm_virtual_network" "{vnet_res}" {{',
        f'  name                = "{hcl_escape(vnet_name)}"',
        f'  address_space       = ["{model.root}"]',
        "  location            = var.location",
        "  resource_group_name = var.resource_group_name",
    ]
    lines += hcl_tags_block(_tf_tags_for(model, model.root))
    lines.append("}")

    used_names: set[str] = set()
    for c in model.leaf_cidrs():
        if c == model.root:
            continue
        name_tag, _ = CIDRModel.split_name_tag(model.get_tags(c))
        res = namer.claim(tf_identifier(name_tag or "", cidr_label("subnet", c)))
        fallback = "snet-" + re.sub(r"[^0-9a-zA-Z]+", "-", c).strip("-")
        special = _AZURE_SPECIAL_SUBNETS.get((name_tag or "").lower())
        name = special[0] if special else _azure_name(name_tag or "", fallback)
        base, i = name, 2
        while name.lower() in used_names:
            name = f"{base}-{i}"
            i += 1
        used_names.add(name.lower())

        lines += [
            "",
            f'resource "azurerm_subnet" "{res}" {{',
            f'  name                 = "{hcl_escape(name)}"',
            "  resource_group_name  = var.resource_group_name",
            f"  virtual_network_name = azurerm_virtual_network.{vnet_res}.name",
            f'  address_prefixes     = ["{c}"]',
        ]
        all_tags = _tf_tags_for(model, c)
        if all_tags:
            lines.append("  # Tags (azurerm_subnet has no tags argument;")
            lines.append("  #  set these on the VNet or resource group instead):")
            for k, v in all_tags.items():
                lines.append(f'  # {k} = "{hcl_escape(v)}"')
        lines.append("}")
    return "\n".join(lines) + "\n"


def render_gcp_tf(model: CIDRModel, plan_name: str) -> str:
    """Terraform for a GCP VPC network and its subnetworks (IPv4).

    Subnetworks go in var.region unless tagged region=<region>. Neither
    resource takes labels, so tags become comments.
    """
    errors, warnings = plan_issues(model, "gcp")
    if errors:
        raise ExportBlocked("gcp", errors)

    namer = UniqueNamer()
    net_tag, _ = CIDRModel.split_name_tag(model.get_tags(model.root))
    network_res = namer.claim(tf_identifier(net_tag or plan_name, "main"))

    lines = [
        "# CIDRSculpter: Terraform / GCP",
        f"# Plan:      {plan_name}",
        f"# Root CIDR: {model.root}",
    ]
    lines += _issue_header(warnings)
    lines += [
        "",
        'variable "project_id" {',
        '  description = "GCP project ID"',
        "  type        = string",
        "}",
        "",
        'variable "region" {',
        '  description = "Default GCP region for subnetworks '
        '(a subnet tagged region=<region> overrides it)"',
        "  type        = string",
        '  default     = "us-central1"',
        "}",
        "",
        f'resource "google_compute_network" "{network_res}" {{',
        f'  name                    = "{_gcp_name(network_res)}"',
        "  project                 = var.project_id",
        "  auto_create_subnetworks = false",
    ]
    lines += _tag_comment_block(_tf_tags_for(model, model.root))
    lines.append("}")

    for c in model.leaf_cidrs():
        if c == model.root:
            continue
        name_tag, _ = CIDRModel.split_name_tag(model.get_tags(c))
        res = namer.claim(tf_identifier(name_tag or "", cidr_label("subnet", c)))
        region = _tag_lookup(model.get_tags(c), "region")
        region_expr = f'"{hcl_escape(region)}"' if region else "var.region"
        lines += [
            "",
            f'resource "google_compute_subnetwork" "{res}" {{',
            f'  name          = "{_gcp_name(res)}"',
            "  project       = var.project_id",
            f"  region        = {region_expr}",
            f'  ip_cidr_range = "{c}"',
            f"  network       = google_compute_network.{network_res}.id",
        ]
        lines += _tag_comment_block(_tf_tags_for(model, c))
        lines.append("}")
    return "\n".join(lines) + "\n"


##=========================================================
## SCREENS
##=========================================================


class PlanNameInput(Screen):
    BINDINGS = [("ctrl+o", "load", "Load existing")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Enter a name for this plan (used as filename prefix and diagram title)\n"
            "[dim]Spaces→underscores · dots/slashes/colons→hyphens in filenames · "
            "ctrl+o to load an existing plan instead[/dim]"
        )
        self.input = Input(placeholder="e.g. Production Network")
        yield self.input
        yield Footer()

    def on_mount(self):
        self.input.focus()

    def on_input_submitted(self):
        name = self.input.value.strip()
        if not name:
            self.app.bell()
            return
        self.app.plan_name = name
        self.app.title = name
        self.app.pop_screen()
        self.app.push_screen(CIDRInput())

    def action_load(self):
        self.app.push_screen(LoadScreen())


class CIDRInput(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Enter starting CIDR: IPv4 or IPv6\n"
            "[dim]e.g.  10.0.0.0/20   or   2001:db8::/32[/dim]"
        )
        self.input = Input(placeholder="e.g. 10.0.0.0/20  or  2001:db8::/32")
        yield self.input
        yield Footer()

    def on_mount(self):
        self.input.focus()

    def on_input_submitted(self):
        cidr = self.input.value.strip()
        try:
            ipaddress.ip_network(cidr, strict=False)
        except Exception:
            self.app.bell()
            return
        self.app.start_model(cidr)
        self.app.sub_title = cidr
        self.app.pop_screen()


class TagInput(Screen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, cidr: str):
        super().__init__()
        self.cidr = cidr

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Tags for [b]{self.cidr}[/b]  (key=value,key2=value2)\n"
            f"[dim]'name'/'Name' is special: used as the Terraform resource name "
            f"and shown distinctly in the diagram.[/dim]"
        )
        existing = self.app.model.get_tags(self.cidr)
        self.input = Input(
            value=CIDRModel.tags_to_string(existing),
            placeholder="name=bastion,private=false,tier=datastore",
        )
        yield self.input
        yield Footer()

    def on_mount(self):
        self.input.focus()
        self.input.cursor_position = len(self.input.value)

    def on_input_submitted(self):
        self.app.model.set_tags(self.cidr, CIDRModel.parse_tag_string(self.input.value))
        self.app.update_details(self.cidr)
        self.app.pop_screen()

    def action_cancel(self):
        self.app.pop_screen()


class TableScreen(Screen):
    BINDINGS = [("q", "close", "Back"), ("escape", "close", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        self.table = DataTable()
        yield self.table
        yield Footer()

    def on_mount(self):
        model = self.app.model
        self.table.add_columns(
            "CIDR",
            "START",
            "END",
            "HOSTS",
            "AWS/AZURE/GCP",
            "PARENT",
            "DEPTH",
            "LEAF",
            "NAME",
            "TAGS",
        )
        for cidr in sorted(model.nodes, key=lambda c: ipaddress.ip_network(c)):
            i = model.info(cidr)
            name, others = CIDRModel.split_name_tag(i["tags"])
            self.table.add_row(
                i["cidr"],
                i["start"],
                i["end"],
                str(i["hosts"]),
                "/".join(str(i["usable"][c]) for c in CLOUDS) if i["usable"] else "-",
                model.parent(cidr),
                str(model.depth(cidr)),
                "yes" if model.is_leaf(cidr) else "no",
                name or "",
                CIDRModel.tags_to_string(others),
            )

    def action_close(self):
        self.app.pop_screen()


##=========================================================
## LOAD SCREEN
##=========================================================


class LoadScreen(Screen):
    """Accept a path to a previously saved *_plan.json and restore it."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Enter path to a saved plan .json file\n"
            "[dim]Plans are saved as  <Plan_Name>_plan.json  "
            "in the current working directory[/dim]"
        )
        self.path_input = Input(placeholder="e.g. Production_Network_plan.json")
        yield self.path_input
        self.error_msg = Static("")
        yield self.error_msg
        yield Footer()

    def on_mount(self):
        self.path_input.focus()

    def on_input_submitted(self):
        path = self.path_input.value.strip()
        if not path:
            self.app.bell()
            return
        try:
            with open(path) as f:
                data = json.load(f)
            # basic validation
            if "root_cidr" not in data or "nodes" not in data:
                raise ValueError("missing required keys: root_cidr / nodes")
            self.app.load_plan(data)
            self.app.pop_screen()
        except FileNotFoundError:
            self.error_msg.update(f"[red]File not found: {path}[/red]")
            self.app.bell()
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            self.error_msg.update(f"[red]Invalid plan file: {exc}[/red]")
            self.app.bell()

    def action_cancel(self):
        self.app.pop_screen()


##=========================================================
## MAIN APP
##=========================================================


class CIDRSculpterApp(App):
    TITLE = "CIDRSculpter"

    CSS = f"""
    Screen {{
        background: {CATPPUCCIN["bg"]};
        color: {CATPPUCCIN["fg"]};
    }}
    Tree {{
        height: 60%;
        border: solid {CATPPUCCIN["blue"]};
    }}
    #details {{
        height: 40%;
        border: solid {CATPPUCCIN["green"]};
        padding: 1;
        overflow-y: scroll;
    }}
    """

    BINDINGS = [
        ("s", "split", "Split"),
        ("j", "join", "Join"),
        ("t", "table", "Table"),
        ("a", "add_tag", "Tag"),
        ("ctrl+s", "save", "Save"),
        ("ctrl+o", "load", "Load"),
        ("1", "export_json", "JSON"),
        ("2", "export_terraform", "Terraform/AWS"),
        ("3", "export_aws", "AWS Plan"),
        ("4", "export_csv", "CSV"),
        ("5", "export_graphviz", "Graphviz"),
        ("6", "export_text", "Plain Text"),
        ("7", "export_markdown", "Markdown"),
        ("8", "export_adf", "ADF"),
        ("9", "export_azure", "Azure TF"),
        ("0", "export_gcp", "GCP TF"),
        ("c", "export_confluence", "Confluence"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.model: CIDRModel | None = None
        self.plan_name: str = "plan"

    ##=========================================================
    ## compose / lifecycle
    ##=========================================================

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            self.cidr_tree = Tree("CIDR SPACE")
            self.cidr_tree.show_root = False
            yield self.cidr_tree
            self.details_panel = Static(
                "Enter a plan name and CIDR to begin", id="details"
            )
            yield self.details_panel
        yield Footer()

    def on_mount(self):
        self.push_screen(PlanNameInput())

    def start_model(self, cidr: str):
        self.model = CIDRModel(cidr)
        self.build_tree()

    def export_path(self, suffix: str) -> str:
        return f"{filename_safe(self.plan_name)}_{suffix}"

    ##=========================================================
    ## tree
    ##=========================================================

    def _tree_label(self, cidr: str) -> Text:
        if self.model.is_leaf(cidr):
            return Text(cidr, style="bold")
        return Text(f"{cidr}  (split)", style="dim italic")

    def build_tree(self, focus_cidr: str | None = None):
        if not self.model:
            return
        self.cidr_tree.clear()
        line_counter = [0]
        cidr_to_line: dict[str, int] = {}

        def record(cidr):
            cidr_to_line[cidr] = line_counter[0]
            line_counter[0] += 1

        root = self.cidr_tree.root.add(
            self._tree_label(self.model.root), data=self.model.root
        )
        root.expand()
        record(self.model.root)

        def add(node, cidr):
            for child in self.model.nodes[cidr].children:
                child_node = node.add(self._tree_label(child), data=child)
                child_node.expand()
                record(child)
                add(child_node, child)

        add(root, self.model.root)
        target = focus_cidr if focus_cidr in cidr_to_line else self.model.root
        self.cidr_tree.cursor_line = cidr_to_line.get(target, 0)
        self.update_details(target)

    def update_details(self, cidr: str | None):
        if not self.model or not isinstance(cidr, str):
            return
        model = self.model
        i = model.info(cidr)
        name, other_tags = CIDRModel.split_name_tag(i["tags"])
        is_leaf = model.is_leaf(cidr)

        lines = [f"[b]CIDR:[/b] {i['cidr']}"]
        if not is_leaf:
            lines.append(
                f"[dim]Split into {len(model.nodes[cidr].children)} children: "
                f"container only, not exported.[/dim]"
            )
        if name:
            lines.append(f"[b]Name:[/b] [{CATPPUCCIN['pink']}]{name}[/]")
        lines += [
            f"[b]Start:[/b] {i['start']}",
            f"[b]End:[/b] {i['end']}",
            f"[b]Hosts:[/b] {_fmt_hosts(i['hosts'])}",
        ]
        if i["usable"]:
            lines.append(
                "[b]Usable in cloud:[/b] "
                + "  ".join(f"{CLOUD_LABEL[c]} {i['usable'][c]}" for c in CLOUDS)
            )
        lines += self._cloud_fit_lines(cidr, is_leaf)
        lines += [
            f"[b]Parent:[/b] {model.parent(cidr)}",
            f"[b]Depth:[/b] {model.depth(cidr)}",
            f"[b]Leaf (usable):[/b] {'yes' if is_leaf else 'no'}",
        ]
        if other_tags:
            tags_block = "\n".join(
                f"  [{CATPPUCCIN['yellow']}]{k}[/] = {v}" for k, v in other_tags.items()
            )
        else:
            tags_block = "  [dim](none: press 'a' to add)[/dim]"
        lines.append(f"[b]Tags:[/b]\n{tags_block}")
        self.details_panel.update("\n".join(lines))

    def _cloud_fit_lines(self, cidr: str, is_leaf: bool) -> list[str]:
        """'Cloud fit: AWS ok  Azure ok  GCP x' plus the reasons, for the
        blocks that get exported (the root as VPC/VNet, and leaf subnets)."""
        model = self.model
        is_root = cidr == model.root
        if not (is_leaf or is_root):
            return []
        marks, reasons = [], []
        for cloud in CLOUDS:
            if is_root and not is_leaf and cloud == "gcp" and not _is_ipv6(cidr):
                marks.append(f"{CLOUD_LABEL[cloud]} [dim]n/a[/dim]")
                continue
            role = "vpc" if is_root else "subnet"
            name, _ = CIDRModel.split_name_tag(model.get_tags(cidr))
            errors, _ = block_issues(cidr, cloud, role, name, check_public=False)
            if errors:
                marks.append(f"{CLOUD_LABEL[cloud]} [{CATPPUCCIN['red']}]x[/]")
                reasons += [
                    f"  [{CATPPUCCIN['red']}]{CLOUD_LABEL[cloud]}:[/] "
                    + e.split(": ", 1)[-1]
                    for e in errors[:1]
                ]
            else:
                marks.append(f"{CLOUD_LABEL[cloud]} [{CATPPUCCIN['green']}]ok[/]")
        what = "VPC/VNet" if is_root else "subnet"
        return [f"[b]Cloud fit ({what}):[/b] " + "  ".join(marks)] + reasons

    ##=========================================================
    ## selection / navigation
    ##=========================================================

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        self.update_details(getattr(event.node, "data", None))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted):
        self.update_details(getattr(event.node, "data", None))

    ##=========================================================
    ## split / join
    ##=========================================================

    def action_split(self):
        if not self.model:
            return
        node = self.cidr_tree.cursor_node
        if not node or not isinstance(node.data, str):
            return
        if self.model.split(node.data):
            self.build_tree(focus_cidr=node.data)
        else:
            self.bell()

    def action_join(self):
        if not self.model:
            return
        node = self.cidr_tree.cursor_node
        if not node or not isinstance(node.data, str):
            return
        if self.model.join(node.data):
            self.build_tree(focus_cidr=node.data)
        else:
            self.bell()

    ##=========================================================
    ## tagging
    ##=========================================================

    def action_add_tag(self):
        if not self.model:
            return
        node = self.cidr_tree.cursor_node
        if not node or not isinstance(node.data, str):
            return
        self.push_screen(TagInput(node.data))

    ##=========================================================
    ## table
    ##=========================================================

    def action_table(self):
        if not self.model:
            return
        self.push_screen(TableScreen())

    ##=========================================================
    ## save / load
    ##=========================================================

    def action_save(self):
        if not self.model:
            self.details_panel.update(
                "[yellow]Nothing to save: enter a CIDR first[/yellow]"
            )
            return
        path = self.export_path("plan.json")
        with open(path, "w") as f:
            json.dump(self.model.to_dict(self.plan_name), f, indent=2)
        self.details_panel.update(f"[green]Saved → {path}[/green]")

    def action_load(self):
        self.push_screen(LoadScreen())

    def load_plan(self, data: dict):
        """Restore state from a save-file dict (called by LoadScreen)."""
        self.plan_name = data.get("plan_name", "plan")
        self.title = self.plan_name
        self.model = CIDRModel.from_dict(data)
        self.sub_title = self.model.root
        self.build_tree()
        self.details_panel.update(
            f"[green]Loaded: {self.plan_name}  ({self.model.root})[/green]"
        )

    ##=========================================================
    ## shared export helpers
    ##=========================================================

    def _export_base(self) -> list[dict]:
        return [
            {
                **self.model.info(c),
                "parent": self.model.parent(c),
                "depth": self.model.depth(c),
                "is_leaf": self.model.is_leaf(c),
            }
            for c in self.model.nodes
        ]

    def _write_cloud_export(self, cloud: str, suffix: str, render) -> None:
        """Render a cloud export and write it, or show why the cloud would
        reject the plan. Nothing is written for a blocked plan, so a stale
        file from an earlier export is never mistaken for this one."""
        try:
            text = render(self.model, self.plan_name)
        except ExportBlocked as blocked:
            items = "\n".join(
                f"  [{CATPPUCCIN['red']}]x[/] {e}" for e in blocked.errors
            )
            self.details_panel.update(
                f"[b][{CATPPUCCIN['red']}]Not exported: "
                f"{CLOUD_LABEL[cloud]} would reject this plan[/][/b]\n{items}\n"
                "[dim]Split or join blocks until these are gone, then export "
                "again.[/dim]"
            )
            self.bell()
            return
        path = self.export_path(suffix)
        with open(path, "w") as f:
            f.write(text)
        _, warnings = plan_issues(self.model, cloud)
        msg = f"[green]Exported {CLOUD_LABEL[cloud]} Terraform → {path}[/green]"
        if warnings:
            msg += "\n" + "\n".join(
                f"  [{CATPPUCCIN['yellow']}]![/] {w}" for w in warnings
            )
        self.details_panel.update(msg)

    ##=========================================================
    ## 1: JSON export
    ##=========================================================

    def action_export_json(self):
        if not self.model:
            return
        with open(self.export_path("cidr.json"), "w") as f:
            json.dump(
                {
                    "plan_name": self.plan_name,
                    "root_cidr": self.model.root,
                    "subnets": self._export_base(),
                },
                f,
                indent=2,
            )
        self.details_panel.update("[green]Exported JSON[/green]")

    ##=========================================================
    ## 2: Terraform / AWS
    ##=========================================================

    def action_export_terraform(self):
        if not self.model:
            return
        self._write_cloud_export("aws", "terraform.tf", render_aws_tf)

    ##=========================================================
    ## 3: AWS JSON plan
    ##=========================================================

    def action_export_aws(self):
        if not self.model:
            return
        plan = render_aws_plan(self.model, self.plan_name)
        path = self.export_path("aws_vpc_plan.json")
        with open(path, "w") as f:
            json.dump(plan, f, indent=2)
        if plan["errors"]:
            self.details_panel.update(
                f"[yellow]Exported AWS Plan → {path}, but AWS would reject it:"
                "[/yellow]\n" + "\n".join(f"  x {e}" for e in plan["errors"])
            )
        else:
            self.details_panel.update(f"[green]Exported AWS Plan → {path}[/green]")

    ##=========================================================
    ## 4: CSV
    ##=========================================================

    def action_export_csv(self):
        if not self.model:
            return
        with open(self.export_path("cidr.csv"), "w", newline="") as f:
            w = csv.writer(f)
            ## # comment lines carry metadata, most CSV parsers skip lines
            ## starting with # or you can strip them: grep -v '^#'
            w.writerow([f"# plan_name: {self.plan_name}"])
            w.writerow([f"# root_cidr: {self.model.root}"])
            w.writerow(
                [
                    "CIDR",
                    "START",
                    "END",
                    "HOSTS",
                    "PARENT",
                    "DEPTH",
                    "LEAF",
                    "NAME",
                    "TAGS",
                    "USABLE_AWS",
                    "USABLE_AZURE",
                    "USABLE_GCP",
                ]
            )
            for row in self._export_base():
                name, others = CIDRModel.split_name_tag(row["tags"])
                w.writerow(
                    [
                        row["cidr"],
                        row["start"],
                        row["end"],
                        row["hosts"],
                        row["parent"],
                        row["depth"],
                        "yes" if row["is_leaf"] else "no",
                        name or "",
                        CIDRModel.tags_to_string(others),
                        *(
                            [row["usable"][c] for c in CLOUDS]
                            if row["usable"]
                            else ["", "", ""]
                        ),
                    ]
                )
        self.details_panel.update("[green]Exported CSV[/green]")

    ##=========================================================
    ## 5: Graphviz
    ##=========================================================

    @staticmethod
    def _dot_text(s: str) -> str:
        return html.escape(str(s), quote=False)

    @staticmethod
    def _cluster_id(cidr: str) -> str:
        return "cluster_" + re.sub(r"[./:]+", "_", cidr)

    @staticmethod
    def _anchor_id(cidr: str, is_leaf: bool) -> str:
        """
        Every node needs one real (rank-able) graphviz node to hang an
        edge off of. A leaf already is one - its label box. A container
        is only a subgraph cluster, and graphviz can't point an edge at
        a cluster directly, so it gets an invisible zero-size point node
        planted inside it to stand in for the cluster during layout.
        """
        if is_leaf:
            return f'"{cidr}"'
        return '"anchor_' + re.sub(r"[./:]+", "_", cidr) + '"'

    def _build_leaf_label(self, cidr: str) -> str:
        info = self.model.info(cidr)
        name, other_tags = CIDRModel.split_name_tag(info["tags"])
        rows = [
            f'<TR><TD ALIGN="CENTER" CELLPADDING="2">'
            f'<FONT FACE="monospace" POINT-SIZE="16" COLOR="{CATPPUCCIN["fg"]}">'
            f"<B>{self._dot_text(cidr)}</B></FONT></TD></TR>"
        ]
        if name:
            rows.append(
                f'<TR><TD ALIGN="CENTER" CELLPADDING="1">'
                f'<FONT FACE="monospace" POINT-SIZE="12" COLOR="{CATPPUCCIN["pink"]}">'
                f"{self._dot_text(name)}</FONT></TD></TR>"
            )
        rows.append(
            f'<TR><TD ALIGN="CENTER" CELLPADDING="1">'
            f'<FONT FACE="monospace" POINT-SIZE="10" COLOR="{CATPPUCCIN["subtext"]}">'
            f"{self._dot_text(info['start'])} - {self._dot_text(info['end'])}</FONT></TD></TR>"
        )
        rows.append(
            f'<TR><TD ALIGN="CENTER" CELLPADDING="1">'
            f'<FONT FACE="monospace" POINT-SIZE="10" COLOR="{CATPPUCCIN["subtext"]}">'
            f"{_fmt_hosts(info['hosts'])} hosts</FONT></TD></TR>"
        )
        for k, v in other_tags.items():
            rows.append(
                f'<TR><TD ALIGN="CENTER" CELLPADDING="0">'
                f'<FONT FACE="monospace" POINT-SIZE="9" COLOR="{CATPPUCCIN["overlay"]}">'
                f"{self._dot_text(k)}={self._dot_text(v)}</FONT></TD></TR>"
            )
        table = (
            '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="1" CELLPADDING="2">'
            + "".join(rows)
            + "</TABLE>"
        )
        return f"<{table}>"

    def _render_node(self, cidr: str, depth: int) -> list[str]:
        node = self.model.nodes[cidr]
        if node.is_leaf:
            name, other_tags = CIDRModel.split_name_tag(node.tags)
            border = (
                CATPPUCCIN["pink"]
                if name
                else CATPPUCCIN["yellow"]
                if other_tags
                else CATPPUCCIN["blue"]
            )
            return [
                f'"{cidr}" [label={self._build_leaf_label(cidr)}, color="{border}"];'
            ]

        shade = CONTAINER_SHADE_CYCLE[depth % len(CONTAINER_SHADE_CYCLE)]
        lines = [f"subgraph {self._cluster_id(cidr)} {{"]
        lines.append(
            f'  label=<<FONT FACE="monospace" POINT-SIZE="13" '
            f'COLOR="{CATPPUCCIN["lavender"]}"><B>{self._dot_text(cidr)}</B></FONT>>;'
        )
        lines.append(
            f'  style="rounded,filled"; color="{CATPPUCCIN["overlay"]}"; '
            f'fillcolor="{shade}"; margin=20; fontname="monospace";'
        )
        lines.append(
            f'  {self._anchor_id(cidr, False)} [shape=point, style=invis, width=0.01, label=""];'
        )
        for child in node.children:
            for line in self._render_node(child, depth + 1):
                lines.append("  " + line)
        lines.append("}")
        return lines

    def _rank_edges(self, cidr: str) -> list[str]:
        """
        Invisible parent -> child edges, one per split. These draw
        nothing (style=invis) - their only job is to give graphviz's
        ranking algorithm a reason to place children below their
        parent, top to bottom, instead of packing every box into a
        single flat row for lack of any edges at all. weight=10 keeps
        each parent roughly centered over its children rather than
        drifting sideways.
        """
        node = self.model.nodes[cidr]
        edges = []
        parent_anchor = self._anchor_id(cidr, node.is_leaf)
        for child in node.children:
            child_anchor = self._anchor_id(child, self.model.is_leaf(child))
            edges.append(f"{parent_anchor} -> {child_anchor} [style=invis, weight=10];")
            edges.extend(self._rank_edges(child))
        return edges

    def action_export_graphviz(self):
        if not self.model:
            return
        lines = [
            "digraph CIDR {",
            f'  bgcolor="{CATPPUCCIN["bg"]}";',
            '  fontname="monospace";',
            f'  label=<<FONT FACE="monospace" POINT-SIZE="22" '
            f'COLOR="{CATPPUCCIN["lavender"]}"><B>{self._dot_text(self.plan_name)}</B>'
            f'</FONT><BR/><FONT FACE="monospace" POINT-SIZE="13" '
            f'COLOR="{CATPPUCCIN["subtext"]}">CIDR Plan: '
            f"{self._dot_text(self.model.root)}</FONT>>;",
            '  rankdir="TB"; labelloc="t"; labeljust="c"; pad=0.4; nodesep=0.4; ranksep=0.5;',
            f'  node [shape=box, style="rounded,filled", '
            f'fillcolor="{CATPPUCCIN["surface"]}", fontname="monospace", '
            f'margin="0.22,0.16", penwidth=2];',
        ]
        lines += self._render_node(self.model.root, 0)
        lines += self._rank_edges(self.model.root)
        lines.append("}")
        with open(self.export_path("cidr.dot"), "w") as f:
            f.write("\n".join(lines))
        self.details_panel.update("[green]Exported Graphviz[/green]")

    ##=========================================================
    ## 6: Plain text space-delimited
    ##=========================================================
    ##
    ## Headers use underscores so the entire output is parseable with
    ## awk, cut, or any whitespace-splitting tool:
    ##
    ##   awk 'NR>3 {print $1, $7}' plan_subnets.txt   # CIDR + LEAF columns
    ##
    ## The first two lines are # comments carrying root_cidr and plan_name.

    def action_export_text(self):
        if not self.model:
            return

        headers = [
            "CIDR",
            "START",
            "END",
            "HOSTS",
            "PARENT",
            "DEPTH",
            "IS_LEAF",
            "NAME",
            "OTHER_TAGS",
        ]

        rows: list[list[str]] = []
        for cidr in sorted(self.model.nodes, key=lambda c: ipaddress.ip_network(c)):
            i = self.model.info(cidr)
            name, others = CIDRModel.split_name_tag(i["tags"])
            rows.append(
                [
                    i["cidr"],
                    i["start"],
                    i["end"],
                    _fmt_hosts(i["hosts"]),
                    self.model.parent(cidr),
                    str(self.model.depth(cidr)),
                    "yes" if self.model.is_leaf(cidr) else "no",
                    name or "-",
                    CIDRModel.tags_to_string(others) or "-",
                ]
            )

        ## Column widths from content
        widths = [
            max(len(headers[i]), *(len(r[i]) for r in rows))
            for i in range(len(headers))
        ]

        def fmt(cells: list[str]) -> str:
            return " ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

        lines = [
            f"# plan_name: {self.plan_name}",
            f"# root_cidr: {self.model.root}",
            fmt(headers),
        ] + [fmt(r) for r in rows]

        with open(self.export_path("subnets.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        self.details_panel.update("[green]Exported plain text[/green]")

    ##=========================================================
    ## 7: Markdown
    ##=========================================================
    ##
    ## Ready to paste directly into GitHub, GitLab, Notion, etc.
    ## Only leaf CIDRs are in the table (containers are not deployable).

    def action_export_markdown(self):
        if not self.model:
            return

        headers = ["CIDR", "Name", "Start", "End", "Hosts", "Leaf", "Tags"]

        rows: list[list[str]] = []
        for cidr in sorted(self.model.nodes, key=lambda c: ipaddress.ip_network(c)):
            i = self.model.info(cidr)
            name, others = CIDRModel.split_name_tag(i["tags"])
            rows.append(
                [
                    f"`{i['cidr']}`",
                    name or "-",
                    i["start"],
                    i["end"],
                    _fmt_hosts(i["hosts"]),
                    "✓" if self.model.is_leaf(cidr) else "",
                    CIDRModel.tags_to_string(others) or "-",
                ]
            )

        widths = [
            max(len(headers[i]), *(len(r[i]) for r in rows))
            for i in range(len(headers))
        ]

        def md_row(cells: list[str]) -> str:
            return (
                "| "
                + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
                + " |"
            )

        sep = "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"

        lines = [
            f"# {self.plan_name}",
            "",
            f"**Root CIDR:** `{self.model.root}`",
            "",
            md_row(headers),
            sep,
        ] + [md_row(r) for r in rows]

        with open(self.export_path("subnets.md"), "w") as f:
            f.write("\n".join(lines) + "\n")
        self.details_panel.update("[green]Exported Markdown[/green]")

    ##=========================================================
    ## 8: ADF (Atlassian Document Format)
    ##=========================================================
    ##
    ## Paste the content of the .adf.json file into Confluence via the
    ## /wiki/rest/api/content endpoint, or use it with the Jira rich-
    ## text editor API. The document includes a heading, the root CIDR
    ## as inline code, and a table of all CIDR blocks.

    def action_export_adf(self):
        if not self.model:
            return

        def _text(t: str, bold: bool = False, code: bool = False) -> dict:
            node: dict = {"type": "text", "text": t}
            marks = []
            if bold:
                marks.append({"type": "strong"})
            if code:
                marks.append({"type": "code"})
            if marks:
                node["marks"] = marks
            return node

        def _para(*content) -> dict:
            return {"type": "paragraph", "content": list(content)}

        def _th(text: str) -> dict:
            return {
                "type": "tableHeader",
                "attrs": {
                    "colspan": 1,
                    "rowspan": 1,
                    "colwidth": None,
                    "background": None,
                },
                "content": [_para(_text(text, bold=True))],
            }

        def _td(text: str) -> dict:
            return {
                "type": "tableCell",
                "attrs": {
                    "colspan": 1,
                    "rowspan": 1,
                    "colwidth": None,
                    "background": None,
                },
                "content": [_para(_text(text))],
            }

        headers = ["CIDR", "Name", "Start", "End", "Hosts", "Leaf", "Tags"]
        header_row = {
            "type": "tableRow",
            "content": [_th(h) for h in headers],
        }

        data_rows = []
        for cidr in sorted(self.model.nodes, key=lambda c: ipaddress.ip_network(c)):
            i = self.model.info(cidr)
            name, others = CIDRModel.split_name_tag(i["tags"])
            cells = [
                i["cidr"],
                name or "-",
                i["start"],
                i["end"],
                _fmt_hosts(i["hosts"]),
                "yes" if self.model.is_leaf(cidr) else "no",
                CIDRModel.tags_to_string(others) or "-",
            ]
            data_rows.append(
                {
                    "type": "tableRow",
                    "content": [_td(c) for c in cells],
                }
            )

        doc = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [_text(self.plan_name)],
                },
                _para(
                    _text("Root CIDR: ", bold=True),
                    _text(self.model.root, code=True),
                ),
                {
                    "type": "table",
                    "attrs": {
                        "isNumberColumnEnabled": False,
                        "layout": "default",
                        "localId": str(uuid.uuid4()),
                    },
                    "content": [header_row] + data_rows,
                },
            ],
        }

        with open(self.export_path("subnets.adf.json"), "w") as f:
            json.dump(doc, f, indent=2)
        self.details_panel.update("[green]Exported ADF (Confluence/Jira)[/green]")

    ##=========================================================
    ## c: Confluence wiki markup
    ##=========================================================
    ##
    ## Classic Confluence storage format / wiki markup table.
    ## Paste directly into the Confluence editor in "wiki markup" mode
    ## (Edit page → Insert → Markup → Confluence Wiki).
    ## Also compatible with the older Confluence wiki editor.
    ##
    ## Format:
    ##   h1. Title
    ##   *Root CIDR:* +10.0.0.0/20+
    ##   ||Header1||Header2||...||
    ##   |cell|cell|...|

    def action_export_confluence(self):
        if not self.model:
            return

        headers = ["CIDR", "Name", "Start", "End", "Hosts", "Leaf", "Tags"]

        def wiki_row(cells: list[str], header: bool = False) -> str:
            sep = "||" if header else "|"
            inner = sep.join(cells)
            return f"{sep}{inner}{sep}"

        lines = [
            f"h1. {self.plan_name}",
            "",
            f"*Root CIDR:* +{self.model.root}+",
            "",
            wiki_row(headers, header=True),
        ]

        for cidr in sorted(self.model.nodes, key=lambda c: ipaddress.ip_network(c)):
            i = self.model.info(cidr)
            name, others = CIDRModel.split_name_tag(i["tags"])
            lines.append(
                wiki_row(
                    [
                        "{{" + i["cidr"] + "}}",
                        name or "-",
                        i["start"],
                        i["end"],
                        _fmt_hosts(i["hosts"]),
                        "(/)" if self.model.is_leaf(cidr) else "(x)",
                        CIDRModel.tags_to_string(others) or "-",
                    ]
                )
            )

        with open(self.export_path("subnets.confluence"), "w") as f:
            f.write("\n".join(lines) + "\n")
        self.details_panel.update("[green]Exported Confluence wiki markup[/green]")

    ##=========================================================
    ## 9: Azure Terraform   (see render_azure_tf)
    ## 0: GCP Terraform     (see render_gcp_tf)
    ##=========================================================

    def action_export_azure(self):
        if not self.model:
            return
        self._write_cloud_export("azure", "azure.tf", render_azure_tf)

    def action_export_gcp(self):
        if not self.model:
            return
        self._write_cloud_export("gcp", "gcp.tf", render_gcp_tf)


##=========================================================
## ENTRY POINT
##=========================================================


def main():
    """Entry point for the installed `cidrsculpt` console script."""
    CIDRSculpterApp().run()


if __name__ == "__main__":
    main()
