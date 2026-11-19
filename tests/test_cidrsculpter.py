"""Tests for cidrsculpter. Run with:  python3 -m pytest -q tests

Needs: pytest, rich, textual. The last section drives the real app headless
through Textual's test pilot.
"""

import asyncio
import importlib.util
import ipaddress
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "cidrsculpter.py"
_spec = importlib.util.spec_from_file_location("cidrsculpter", SCRIPT)
cs = importlib.util.module_from_spec(_spec)
sys.modules["cidrsculpter"] = cs  # dataclasses need the module registered
_spec.loader.exec_module(cs)


def model(root, *splits, tags=None):
    m = cs.CIDRModel(root)
    for c in splits:
        assert m.split(c), c
    for cidr, t in (tags or {}).items():
        m.set_tags(cidr, t)
    return m


def split_to(m, prefixlen):
    """Split every leaf until all leaves are /prefixlen."""
    while any(ipaddress.ip_network(c).prefixlen < prefixlen for c in m.leaf_cidrs()):
        for c in m.leaf_cidrs():
            if ipaddress.ip_network(c).prefixlen < prefixlen:
                m.split(c)
    return m


def tf_labels(text):
    return re.findall(r'^resource "[^"]+" "([^"]+)"', text, re.M)


TF_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# ------------------------------------------------------------------ model


def test_split_and_join():
    m = model("10.0.0.0/24", "10.0.0.0/24")
    assert m.leaf_cidrs() == ["10.0.0.0/25", "10.0.0.128/25"]
    assert m.join("10.0.0.0/24")
    assert m.leaf_cidrs() == ["10.0.0.0/24"]


def test_save_load_round_trip():
    m = model("10.0.0.0/24", "10.0.0.0/24", tags={"10.0.0.0/25": {"name": "a"}})
    m2 = cs.CIDRModel.from_dict(json.loads(json.dumps(m.to_dict("p"))))
    assert m2.leaf_cidrs() == m.leaf_cidrs()
    assert m2.get_tags("10.0.0.0/25") == {"name": "a"}


# ---------------------------------------------------------- usable hosts


@pytest.mark.parametrize(
    "cidr,generic,aws,azure,gcp",
    [
        ("10.0.0.0/24", 254, 251, 251, 252),
        ("10.0.0.0/28", 14, 11, 11, 12),
        ("10.0.0.0/29", 6, 3, 3, 4),
    ],
)
def test_usable_hosts_per_cloud(cidr, generic, aws, azure, gcp):
    assert cs.usable_hosts(cidr) == generic
    assert cs.usable_hosts(cidr, "aws") == aws
    assert cs.usable_hosts(cidr, "azure") == azure
    assert cs.usable_hosts(cidr, "gcp") == gcp


def test_info_carries_cloud_usable_for_ipv4_only():
    assert cs.CIDRModel("10.0.0.0/24").info("10.0.0.0/24")["usable"] == {
        "aws": 251,
        "azure": 251,
        "gcp": 252,
    }
    assert cs.CIDRModel("2001:db8::/64").info("2001:db8::/64")["usable"] is None


# ------------------------------------------------------------ cloud rules


@pytest.mark.parametrize(
    "cidr,role,cloud,ok",
    [
        ("10.0.0.0/16", "vpc", "aws", True),
        ("10.0.0.0/15", "vpc", "aws", False),  # too big for a VPC
        ("10.0.0.0/28", "subnet", "aws", True),
        ("10.0.0.0/29", "subnet", "aws", False),  # smaller than /28
        ("10.0.0.0/29", "subnet", "azure", True),
        ("10.0.0.0/30", "subnet", "azure", False),
        ("10.0.0.0/29", "subnet", "gcp", True),
        ("10.0.0.0/30", "subnet", "gcp", False),
        ("10.0.0.0/8", "vpc", "azure", True),  # Azure VNets can be large
        ("127.0.0.0/24", "subnet", "gcp", False),  # loopback
        ("169.254.0.0/24", "subnet", "azure", False),  # link-local
        ("224.0.0.0/24", "subnet", "aws", False),  # multicast
        ("168.63.129.0/24", "subnet", "azure", False),  # Azure platform IP
        ("168.63.129.0/24", "subnet", "aws", True),
    ],
)
def test_block_rules(cidr, role, cloud, ok):
    errors, _ = cs.block_issues(cidr, cloud, role)
    assert (not errors) == ok, errors


def test_public_range_is_a_warning_not_an_error():
    errors, warnings = cs.block_issues("8.8.8.0/24", "aws", "subnet")
    assert errors == []
    assert any("RFC 1918" in w for w in warnings)


def test_docker_bridge_warning_on_aws():
    _, warnings = cs.block_issues("172.17.0.0/16", "aws", "vpc")
    assert any("Docker" in w for w in warnings)


@pytest.mark.parametrize(
    "name,prefix,ok",
    [
        ("AzureBastionSubnet", 26, True),
        ("AzureBastionSubnet", 27, False),
        ("azurefirewallsubnet", 27, False),
        ("GatewaySubnet", 29, True),
        ("anything-else", 29, True),
    ],
)
def test_azure_special_subnet_sizes(name, prefix, ok):
    errors, _ = cs.block_issues(f"10.0.0.0/{prefix}", "azure", "subnet", name)
    assert (not errors) == ok


@pytest.mark.parametrize(
    "cidr,role,ok",
    [
        ("2001:db8::/56", "vpc", True),
        ("2001:db8::/48", "vpc", False),  # Amazon gives /56
        ("2001:db8::/60", "subnet", True),
        ("2001:db8::/64", "subnet", True),
        ("2001:db8::/62", "subnet", False),  # not a multiple of 4
        ("2001:db8::/65", "subnet", False),  # smaller than /64
    ],
)
def test_aws_ipv6_rules(cidr, role, ok):
    errors, _ = cs.block_issues(cidr, "aws", role)
    assert (not errors) == ok, errors


@pytest.mark.parametrize("cloud", ["azure", "gcp"])
def test_ipv6_only_plans_are_blocked_on_azure_and_gcp(cloud):
    m = split_to(cs.CIDRModel("2001:db8::/56"), 64)
    errors, _ = cs.plan_issues(m, cloud)
    assert len(errors) == 1  # one clear explanation, not 256


def test_plan_issues_reports_public_warning_once():
    m = split_to(cs.CIDRModel("8.8.0.0/20"), 24)
    _, warnings = cs.plan_issues(m, "aws")
    assert sum("RFC 1918" in w for w in warnings) == 1


# --------------------------------------------------------- HCL helpers


def test_tags_block_quotes_keys_and_escapes_templates():
    lines = cs.hcl_tags_block(
        {"team name": "core", "app:tier": "web", "x": '${var.a} %{if} "q"\n'}
    )
    text = "\n".join(lines)
    assert '"team name" = "core"' in text.replace("  ", " ").replace("  ", " ")
    assert '"app:tier"' in text
    assert "$${var.a}" in text and "%%{if}" in text
    assert '\\"q\\"' in text and "\\n" in text


def test_cidr_label_never_starts_with_digit():
    assert cs.cidr_label("subnet", "10.0.1.0/24") == "subnet_10_0_1_0_24"
    assert cs.cidr_label("subnet", "2001:db8::/64") == "subnet_2001_db8_64"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Private Apps", "Private-Apps"),
        ("_leading", "leading"),
        ("trailing.-", "trailing"),
        ("GatewaySubnet", "GatewaySubnet"),
        ("***", "fallback"),
    ],
)
def test_azure_name(raw, expected):
    assert cs._azure_name(raw, "fallback") == expected


# ------------------------------------------------------------ AWS render


def v4_plan():
    return model(
        "10.20.0.0/20",
        "10.20.0.0/20",
        "10.20.0.0/21",
        tags={
            "10.20.8.0/21": {"name": "Private Apps", "az": "us-east-1c"},
            "10.20.0.0/22": {"team name": "core", "note": "${x}"},
        },
    )


def test_aws_ipv4_render():
    text = cs.render_aws_tf(v4_plan(), "Prod VPC")
    assert all(TF_LABEL.match(label) for label in tf_labels(text))
    assert 'resource "aws_subnet" "subnet_10_20_0_0_22"' in text
    assert 'resource "aws_subnet" "private_apps"' in text
    assert 'availability_zone = "us-east-1c"' in text
    assert "element(var.availability_zones, 0)" in text
    assert "element(var.availability_zones, 1)" in text
    assert 'variable "availability_zones"' in text
    assert '"$${x}"' in text


def test_aws_no_az_variable_when_every_subnet_is_pinned():
    m = model(
        "10.0.0.0/24",
        "10.0.0.0/24",
        tags={"10.0.0.0/25": {"az": "a"}, "10.0.0.128/25": {"availability_zone": "b"}},
    )
    assert "var.availability_zones" not in cs.render_aws_tf(m, "p")


def test_aws_render_blocks_invalid_plan():
    m = split_to(cs.CIDRModel("10.0.0.0/24"), 29)
    with pytest.raises(cs.ExportBlocked) as exc:
        cs.render_aws_tf(m, "p")
    assert len(exc.value.errors) == 32


def test_aws_ipv6_render_places_subnets_by_offset():
    # sixteen /60s, then the first /60 split all the way down to sixteen /64s
    m = split_to(cs.CIDRModel("2001:db8:0:100::/56"), 60)
    m.split("2001:db8:0:100::/60")
    while any(61 <= ipaddress.ip_network(c).prefixlen <= 63 for c in m.leaf_cidrs()):
        for c in m.leaf_cidrs():
            if 61 <= ipaddress.ip_network(c).prefixlen <= 63:
                m.split(c)
    text = cs.render_aws_tf(m, "v6")
    assert "assign_generated_ipv6_cidr_block = true" in text
    assert "ipv6_native" in text
    pairs = re.findall(
        r"# plan: (\S+)\n.*?cidrsubnet\(aws_vpc\.\w+\.ipv6_cidr_block, (\d+), (\d+)\)",
        text,
        re.S,
    )
    assert len(pairs) == len(m.leaf_cidrs()) == 15 + 16
    root = ipaddress.ip_network(m.root)
    for plan, newbits, netnum in pairs:
        # Python equivalent of Terraform's cidrsubnet(root, newbits, netnum)
        got = list(root.subnets(prefixlen_diff=int(newbits)))[int(netnum)]
        assert str(got) == plan


def test_aws_plan_json_reports_validity():
    bad = split_to(cs.CIDRModel("10.0.0.0/24"), 29)
    plan = cs.render_aws_plan(bad, "p")
    assert plan["valid_for_aws"] is False and plan["errors"]
    good = cs.render_aws_plan(v4_plan(), "p")
    assert good["valid_for_aws"] is True
    assert good["subnets"][0]["usable_hosts"] == 1019


# ----------------------------------------------------- Azure / GCP render


def test_azure_render_names():
    m = model(
        "10.20.0.0/20",
        "10.20.0.0/20",
        tags={
            "10.20.0.0/21": {"name": "gatewaysubnet"},
            "10.20.8.0/21": {"name": "_odd name!"},
        },
    )
    text = cs.render_azure_tf(m, "Prod Net")
    assert all(TF_LABEL.match(label) for label in tf_labels(text))
    assert 'name                 = "GatewaySubnet"' in text  # canonical case
    assert 'name                 = "odd-name"' in text
    assert 'name                = "Prod-Net"' in text


def test_azure_unnamed_subnet_names_are_valid():
    m = model("10.20.0.0/20", "10.20.0.0/20")
    text = cs.render_azure_tf(m, "p")
    assert '"snet-10-20-0-0-21"' in text


def test_gcp_render():
    m = model(
        "10.20.0.0/20",
        "10.20.0.0/20",
        tags={"10.20.8.0/21": {"name": "Web Tier", "region": "europe-west1"}},
    )
    text = cs.render_gcp_tf(m, "Prod Net")
    assert all(TF_LABEL.match(label) for label in tf_labels(text))
    assert 'name          = "subnet-10-20-0-0-21"' in text
    assert 'name          = "web-tier"' in text
    assert 'region        = "europe-west1"' in text
    assert "region        = var.region" in text


def test_gcp_root_size_is_not_checked():
    # GCP networks have no range of their own; only subnets are limited.
    m = model("10.0.0.0/8", "10.0.0.0/8")
    assert cs.plan_issues(m, "gcp") == ([], [])


# ------------------------------------------------------- app (headless)


async def _drive(keys_after_start, cidr, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = cs.CIDRSculpterApp()
    async with app.run_test() as pilot:
        await pilot.press(*"Prod", "enter")
        await pilot.press(*cidr, "enter")
        await pilot.pause()
        for key in keys_after_start:
            await pilot.press(key)
            await pilot.pause()
        panel = str(app.details_panel.render())
    return app, panel


def test_app_exports_valid_plan(tmp_path, monkeypatch):
    # split the root, then export AWS, Azure, GCP and CSV
    app, panel = asyncio.run(
        _drive(["s", "2", "9", "0", "4"], "10.0.0.0/20", tmp_path, monkeypatch)
    )
    for f in ("Prod_terraform.tf", "Prod_azure.tf", "Prod_gcp.tf", "Prod_cidr.csv"):
        assert (tmp_path / f).exists(), f
    csv_text = (tmp_path / "Prod_cidr.csv").read_text()
    assert "USABLE_AWS,USABLE_AZURE,USABLE_GCP" in csv_text
    assert ",2043,2043,2044" in csv_text  # a /21 leaf


def test_app_refuses_invalid_aws_export(tmp_path, monkeypatch):
    # a /28 root split once gives two /29s: too small for AWS
    app, panel = asyncio.run(_drive(["s", "2"], "10.0.0.0/28", tmp_path, monkeypatch))
    assert not (tmp_path / "Prod_terraform.tf").exists()
    assert "Not exported" in panel and "/28 at least" in panel


def test_app_details_show_cloud_fit(tmp_path, monkeypatch):
    app, panel = asyncio.run(_drive([], "10.0.0.0/30", tmp_path, monkeypatch))
    assert "Usable in cloud" in panel
    assert "Cloud fit" in panel and "AWS" in panel


# ------------------------------------------------------- review fixes


def test_from_dict_reads_legacy_non_canonical_root():
    # saves made before CIDR input was normalized keyed the root as typed
    data = cs.CIDRModel("10.0.0.0/24").to_dict("p")
    data["root_cidr"] = "10.0.0.5/24"
    data["nodes"] = {"10.0.0.5/24": data["nodes"]["10.0.0.0/24"]}
    assert cs.CIDRModel.from_dict(data).root == "10.0.0.0/24"


@pytest.mark.parametrize(
    "data",
    [
        {"root_cidr": "10.0.0.0/16", "nodes": []},
        {"root_cidr": "10.0.0.0/16", "nodes": {}},
        {"root_cidr": "nope", "nodes": {}},
        {
            "root_cidr": "10.0.0.0/16",
            "nodes": {"10.0.0.0/16": {"children": ["10.0.0.0/16"]}},
        },
        {
            "root_cidr": "10.0.0.0/16",
            "nodes": {"10.0.0.0/16": {"children": ["10.0.0.0/17", "10.0.128.0/17"]}},
        },
    ],
)
def test_from_dict_rejects_malformed(data):
    with pytest.raises(ValueError):
        cs.CIDRModel.from_dict(data)


def test_tag_comments_cannot_inject_hcl():
    m = model("10.0.0.0/16", "10.0.0.0/16")
    m.set_tags("10.0.0.0/17", {'x\nresource "null_resource" "pwn" {}\n#': "v"})
    for render in (cs.render_aws_tf, cs.render_azure_tf, cs.render_gcp_tf):
        text = render(m, 'p\nresource "null_resource" "pwn2" {}')
        assert "null_resource" not in tf_labels(text)
        assert not re.search(r'^resource "null_resource"', text, re.M)


def test_gcp_warns_when_any_subnet_is_public():
    m = model("192.168.0.0/15", "192.168.0.0/15")  # second half is 192.169/16
    _, warnings = cs.plan_issues(m, "gcp")
    assert any("RFC 1918" in w for w in warnings)


def test_aws_name_tag_wins_over_second_name_key():
    m = cs.CIDRModel("10.0.0.0/16")
    m.set_tags(m.root, {"name": "a", "Name": "b"})
    text = cs.render_aws_tf(m, "p")
    assert 'resource "aws_vpc" "a"' in text
    assert '"Name" = "a"' in text and '"b"' not in text


def test_gcp_and_azure_names_stay_unique_after_truncation():
    m = model("10.0.0.0/16", "10.0.0.0/16")
    long = "x" * 90
    m.set_tags("10.0.0.0/17", {"name": long})
    m.set_tags("10.0.128.0/17", {"name": long})
    for render, max_len in ((cs.render_gcp_tf, 63), (cs.render_azure_tf, 80)):
        names = re.findall(r'^  name\s+= "(x[^"]*)"', render(m, "p"), re.M)
        assert len(names) == 2 and len(set(names)) == 2, names
        assert all(len(n) <= max_len for n in names), names


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("owners=a,b,tier=web", {"owners": "a,b", "tier": "web"}),
        ("name=bastion, private=false", {"name": "bastion", "private": "false"}),
        ("k=a=b,c=d", {"k": "a=b", "c": "d"}),
    ],
)
def test_tag_values_keep_commas(raw, expected):
    tags = cs.CIDRModel.parse_tag_string(raw)
    assert tags == expected
    assert cs.CIDRModel.parse_tag_string(cs.CIDRModel.tags_to_string(tags)) == tags


def test_export_base_is_in_address_order():
    m = model("10.0.0.0/24", "10.0.0.0/24", "10.0.0.128/25")
    m.join("10.0.0.128/25")
    m.split("10.0.0.0/25")
    app = cs.CIDRSculpterApp()
    app.model = m
    cidrs = [row["cidr"] for row in app._export_base()]
    assert cidrs == sorted(cidrs, key=ipaddress.ip_network)


def test_app_normalizes_host_bits(tmp_path, monkeypatch):
    app, _ = asyncio.run(
        _drive(["t", "escape", "2"], "10.0.0.5/16", tmp_path, monkeypatch)
    )
    assert app.model.root == "10.0.0.0/16"
    assert (tmp_path / "Prod_terraform.tf").exists()


def test_app_survives_malformed_plan_file(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text('{"root_cidr": "10.0.0.0/16", "nodes": []}')

    async def run():
        monkeypatch.chdir(tmp_path)
        app = cs.CIDRSculpterApp()
        async with app.run_test() as pilot:
            await pilot.press(*"Prod", "enter", *"10.0.0.0/20", "enter")
            await pilot.press("ctrl+o", *"bad.json", "enter")
            await pilot.pause()
            error = str(app.screen.error_msg.render())
        return app, error

    app, error = asyncio.run(run())
    assert "Invalid plan file" in error
    assert app.model.root == "10.0.0.0/20" and app.plan_name == "Prod"


def test_app_load_from_start_screen_lands_on_tree(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(cs.CIDRModel("10.0.0.0/16").to_dict("Saved")))

    async def run():
        monkeypatch.chdir(tmp_path)
        app = cs.CIDRSculpterApp()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o", *"p.json", "enter")
            await pilot.pause()
            stack = len(app.screen_stack)
        return app, stack

    app, stack = asyncio.run(run())
    assert stack == 1 and app.plan_name == "Saved"


def test_app_reports_write_failure(tmp_path, monkeypatch):
    (tmp_path / "Prod_cidr.csv").mkdir()
    _, panel = asyncio.run(_drive(["4"], "10.0.0.0/16", tmp_path, monkeypatch))
    assert "Could not write" in panel


def test_app_text_export_stays_awk_parseable(tmp_path, monkeypatch):
    keys = ["a", *"name=Private Apps,team=core ops", "enter", "6"]
    asyncio.run(_drive(keys, "10.0.0.0/16", tmp_path, monkeypatch))
    lines = (tmp_path / "Prod_subnets.txt").read_text().splitlines()[2:]
    assert {len(line.split()) for line in lines} == {9}


def test_app_table_exports_escape_pipes(tmp_path, monkeypatch):
    keys = ["a", *"name=a|b", "enter", "7", "c"]
    asyncio.run(_drive(keys, "10.0.0.0/16", tmp_path, monkeypatch))
    md = (tmp_path / "Prod_subnets.md").read_text().splitlines()[-1]
    assert "a\\|b" in md and md.count("|") - md.count("\\|") == 8
    wiki = (tmp_path / "Prod_subnets.confluence").read_text().splitlines()[-1]
    assert "a\\|b" in wiki


def test_app_quit_warns_about_unsaved_changes(tmp_path, monkeypatch):
    app, panel = asyncio.run(_drive(["s", "q"], "10.0.0.0/16", tmp_path, monkeypatch))
    assert "Unsaved changes" in panel
    app, _ = asyncio.run(
        _drive(["s", "ctrl+s", "q"], "10.0.0.0/16", tmp_path, monkeypatch)
    )
    assert app.return_code == 0


def test_app_tree_keys_do_nothing_on_table_screen(tmp_path, monkeypatch):
    app, _ = asyncio.run(_drive(["t", "s", "2"], "10.0.0.0/16", tmp_path, monkeypatch))
    assert app.model.is_leaf("10.0.0.0/16")
    assert not (tmp_path / "Prod_terraform.tf").exists()
