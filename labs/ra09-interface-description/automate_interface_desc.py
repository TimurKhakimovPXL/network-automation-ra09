from ncclient import manager
import requests
import urllib3
from requests.auth import HTTPBasicAuth
import json
import yaml
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RESTCONF_HEADERS = {
    "Accept": "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_yaml_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_device_params(device):
    return {
        "host": device["host"],
        "port": device.get("port", 830),
        "username": device["username"],
        "password": device["password"],
        "hostkey_verify": False,
        "device_params": {"name": "csr"},
        "allow_agent": False,
        "look_for_keys": False,
    }


def build_restconf_base(device):
    return f"https://{device['host']}/restconf/data"


def build_restconf_auth(device):
    return HTTPBasicAuth(device["username"], device["password"])


def restconf_get_interface(device, interface_type, interface_name):
    """
    Read one specific interface via RESTCONF.
    Returns parsed JSON.
    Raises requests exceptions on failure.
    """
    base = build_restconf_base(device)
    auth = build_restconf_auth(device)
    encoded_name = quote(interface_name, safe="")
    url = (
    f"{base}/Cisco-IOS-XE-native:native/interface/"
    f"{interface_type}={encoded_name}"
)



    response = requests.get(
        url,
        headers=RESTCONF_HEADERS,
        auth=auth,
        verify=False,
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def extract_description(interface_data, interface_type):
    top_key = f"Cisco-IOS-XE-native:{interface_type}"
    block = interface_data.get(top_key, {})
    return block.get("description")


def netconf_edit_description(device, interface_type, interface_name, description):
    """
    Apply interface description change via NETCONF.
    """
    device_params = build_device_params(device)

    netconf_config = f"""
<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
    <interface>
      <{interface_type}>
        <name>{interface_name}</name>
        <description>{description}</description>
      </{interface_type}>
    </interface>
  </native>
</config>
""".strip()

    with manager.connect(**device_params) as m:
        reply = m.edit_config(target="running", config=netconf_config)
        return str(reply)

def process_change(device, change):
    interface_type = change["interface_type"]
    interface_name = str(change["interface_name"])
    desired_description = change["description"]

    result = {
        "timestamp": now_iso(),
        "device_name": device.get("name", device["host"]),
        "host": device["host"],
        "interface_type": interface_type,
        "interface_name": interface_name,
        "desired_description": desired_description,
        "old_description": None,
        "new_description": None,
        "changed": False,
        "verified": False,
        "status": "unknown",
        "error": None,
    }

    print(f"\n=== Processing {result['device_name']} {interface_type}{interface_name} ===")

    # 1. Read current state
    try:
        current_data = restconf_get_interface(device, interface_type, interface_name)
        current_description = extract_description(current_data, interface_type)
        result["old_description"] = current_description
        print(f"[INFO] Current description: {current_description!r}")

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            result["status"] = "interface_not_found"
            result["error"] = f"Interface {interface_type}{interface_name} not found"
        else:
            result["status"] = "read_failed"
            result["error"] = f"RESTCONF read failed: {e}"

        print(f"[ERROR] {result['error']}")
        return result

    except requests.exceptions.RequestException as e:
        result["status"] = "read_failed"
        result["error"] = f"RESTCONF read failed: {e}"
        print(f"[ERROR] {result['error']}")
        return result

    # 2. Decide whether change is needed
    if current_description == desired_description:
        result["new_description"] = current_description
        result["changed"] = False
        result["verified"] = True
        result["status"] = "already_correct"
        print("[SKIP] Desired description already present. No change needed.")
        return result

    # 3. Apply change
    try:
        reply = netconf_edit_description(
            device, interface_type, interface_name, desired_description
        )
        result["changed"] = True
        print("[INFO] NETCONF edit applied.")
        print(reply)
    except Exception as e:
        result["status"] = "edit_failed"
        result["error"] = f"NETCONF edit failed: {e}"
        print(f"[ERROR] {result['error']}")
        return result

    # 4. Verify change
    try:
        verified_data = restconf_get_interface(device, interface_type, interface_name)
        verified_description = extract_description(verified_data, interface_type)
        result["new_description"] = verified_description

        if verified_description == desired_description:
            result["verified"] = True
            result["status"] = "success"
            print(f"[SUCCESS] Verified description: {verified_description!r}")
        else:
            result["verified"] = False
            result["status"] = "verify_mismatch"
            result["error"] = (
                f"Verification mismatch: expected {desired_description!r}, "
                f"got {verified_description!r}"
            )
            print(f"[WARNING] {result['error']}")
    except requests.exceptions.RequestException as e:
        result["status"] = "verify_failed"
        result["error"] = f"RESTCONF verify failed: {e}"
        print(f"[ERROR] {result['error']}")

    return result


def write_report(report, path="report.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\n[INFO] Report written to {path}")


def main():
    input_file = "changes.yaml"

    if not Path(input_file).exists():
        print(f"[ERROR] Input file not found: {input_file}")
        return

    data = load_yaml_file(input_file)

    devices = data.get("devices", [])
    if not devices:
        print("[ERROR] No devices found in YAML input.")
        return

    all_results = []

    for device in devices:
        changes = device.get("changes", [])
        if not changes:
            print(
                f"[WARNING] No changes defined for device "
                f"{device.get('name', device.get('host'))}"
            )
            continue

        for change in changes:
            result = process_change(device, change)
            all_results.append(result)

    report = {
        "generated_at": now_iso(),
        "total_tasks": len(all_results),
        "success": sum(1 for r in all_results if r["status"] == "success"),
        "already_correct": sum(1 for r in all_results if r["status"] == "already_correct"),
        "failed": sum(
            1
            for r in all_results
            if r["status"] not in ("success", "already_correct")
        ),
        "results": all_results,
    }

    write_report(report, "report.json")


if __name__ == "__main__":
    main()
