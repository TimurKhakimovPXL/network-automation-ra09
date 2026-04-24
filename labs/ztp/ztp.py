"""
ztp.py — Zero Touch Provisioning bootstrap script
PXL DEVNET lab — Cisco IOS XE ISR4200 (16.8+)

Delivery:  TFTP server at 10.199.64.134
Trigger:   DHCP option 67 → tftp://10.199.64.134/ztp.py

Execution environment: IOS XE Guest Shell (Python 3.6+)
IOS XE CLI access via the built-in 'cli' module.

How device identification works (fully automatic, no manual MAC list):
  The device already received a DHCP lease before ZTP runs.
  This script reads the assigned IP from 'show interface Gig0/0/0',
  then derives rack number, side (C01/C02), hostname, and full config
  from the documented PXL addressing scheme:

    Management subnet:  172.17.X.0/28   (C01, left side)
                        172.17.X.64/28  (C02, right side)
    Router mgmt IP:     172.17.X.2      (C01)
                        172.17.X.66     (C02)
    Gateway:            172.17.X.1      (C01)
                        172.17.X.65     (C02)

  Where X = rack number (1-10), derived from the third octet of the DHCP IP.

What this script produces:
  - Hostname (LAB-RA0X-C01/C02-R01)
  - Enable secret + local admin credentials
  - Management interface IP (static, matching DHCP assignment)
  - Default route via rack gateway
  - Domain name + RSA 2048 key (with 16.8 compatibility check)
  - SSH v2
  - VTY SSH-only access
  - NETCONF-YANG
  - RESTCONF
  - write memory

Log: bootflash:ztp.log (persists across reboots for troubleshooting)
"""

import cli
import os
import re
import sys
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────
# Credentials are read from environment variables.
# For lab use, defaults are provided — override in production.

DOMAIN_NAME   = os.environ.get("ZTP_DOMAIN",   "data.labnet.local")
ENABLE_SECRET = os.environ.get("ZTP_SECRET",   "cisco")
ADMIN_USER    = os.environ.get("ZTP_USER",     "admin")
ADMIN_PASS    = os.environ.get("ZTP_PASS",     "cisco")
LOG_PATH      = os.environ.get("ZTP_LOG_PATH", "/bootflash/ztp.log")

# Management interface — ISR4200 uses Gig0/0/0 as the management-side port
MGMT_INTERFACE = "GigabitEthernet0/0/0"


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    """Write a timestamped message to bootflash:ztp.log and stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # Never abort ZTP due to a logging failure


# ── Device identification ─────────────────────────────────────────────────────

def get_dhcp_ip():
    """
    Read the IP address currently assigned to the management interface
    by DHCP. ZTP only runs after the device has received a DHCP lease,
    so this IP is guaranteed to be present.

    Returns the IP string e.g. '172.17.9.2', or None on failure.
    """
    output = cli.execute(f"show interface {MGMT_INTERFACE}")

    # Match: Internet address is 172.17.9.2/28
    match = re.search(r"Internet address is (\d+\.\d+\.\d+\.\d+)/\d+", output)
    if match:
        return match.group(1)
    return None


def derive_device_config(dhcp_ip):
    """
    Derive the full device config from the DHCP-assigned IP using the
    PXL lab addressing scheme. No manual MAC inventory required.

    Addressing logic:
      172.17.X.2  -> C01-R01 (left side, management subnet 172.17.X.0/28)
      172.17.X.66 -> C02-R01 (right side, management subnet 172.17.X.64/28)

    The rack number X is the third octet of the IP.
    The fourth octet determines which router: C01=.2, C02=.66

    Returns a config dict or None if the IP does not match the scheme.
    """
    parts = dhcp_ip.split(".")
    if len(parts) != 4:
        return None

    try:
        rack = int(parts[2])   # third octet = rack number
        host = int(parts[3])   # fourth octet = host position
    except ValueError:
        return None

    if rack < 1 or rack > 10:
        log(f"[WARN] Rack number {rack} is outside expected range 1-10.")

    # C01-R01: left side, .0/28 subnet, router is .2, gateway is .1
    if host == 2:
        return {
            "hostname":    f"LAB-RA{rack:02d}-C01-R01",
            "mgmt_ip":     dhcp_ip,
            "mgmt_mask":   "255.255.255.240",
            "mgmt_prefix": "28",
            "gateway":     f"172.17.{rack}.1",
            "rack":        rack,
            "side":        "C01",
        }

    # C02-R01: right side, .64/28 subnet, router is .66, gateway is .65
    elif host == 66:
        return {
            "hostname":    f"LAB-RA{rack:02d}-C02-R01",
            "mgmt_ip":     dhcp_ip,
            "mgmt_mask":   "255.255.255.240",
            "mgmt_prefix": "28",
            "gateway":     f"172.17.{rack}.65",
            "rack":        rack,
            "side":        "C02",
        }

    else:
        log(f"[ERROR] Host octet {host} does not match C01 (.2) or C02 (.66) pattern.")
        log("Check DHCP reservations — device may have received an unexpected IP.")
        return None


# ── RSA key generation ────────────────────────────────────────────────────────

def rsa_key_exists():
    """
    Check whether an RSA key pair already exists on the device.
    Avoids re-generating a key on devices that already have one,
    and works around interactive prompts on some IOS XE 16.8 builds.
    """
    output = cli.execute("show crypto key mypubkey rsa")
    return "key name" in output.lower()


def generate_rsa_key():
    """
    Generate a 2048-bit RSA key pair if one does not already exist.

    RSA key generation is an exec-level command — it must be called via
    cli.execute(), not cli.configurep(). On IOS XE 16.8 some builds show
    an interactive confirmation prompt that can hang Guest Shell. The
    rsa_key_exists() pre-check avoids triggering generation unnecessarily.
    """
    if rsa_key_exists():
        log("RSA key already exists — skipping generation.")
        return True

    log("Generating RSA 2048-bit key pair...")
    try:
        output = cli.execute("crypto key generate rsa modulus 2048")
        log(f"RSA output: {output.strip()}")

        if rsa_key_exists():
            log("[OK] RSA key confirmed.")
            return True
        else:
            log("[WARN] RSA key generation ran but key not confirmed in output.")
            log("SSH may require manual key generation after bootstrap.")
            return False

    except Exception as e:
        log(f"[WARN] RSA key generation raised exception: {e}")
        log("Continuing — SSH will require manual key generation.")
        return False


# ── Config push ───────────────────────────────────────────────────────────────

def apply_config(device):
    """
    Push day-0 bootstrap configuration via cli.configurep().

    cli.configurep() accepts a list of IOS config commands exactly as
    typed in 'configure terminal', without 'conf t' or 'end'.

    The management IP is set as a static address matching the DHCP-assigned
    value. This ensures the IP survives DHCP lease expiry or server restart
    before the Day-N automation controller pushes the full config.
    """
    hostname  = device["hostname"]
    mgmt_ip   = device["mgmt_ip"]
    mgmt_mask = device["mgmt_mask"]
    gateway   = device["gateway"]

    log(f"Pushing bootstrap config for {hostname}...")

    config = [
        # ── Identity ──────────────────────────────────────────────────────────
        f"hostname {hostname}",
        f"ip domain-name {DOMAIN_NAME}",

        # ── Credentials ───────────────────────────────────────────────────────
        f"enable secret {ENABLE_SECRET}",
        f"username {ADMIN_USER} privilege 15 secret {ADMIN_PASS}",

        # ── Management interface — static IP matching DHCP assignment ─────────
        f"interface {MGMT_INTERFACE}",
        f" ip address {mgmt_ip} {mgmt_mask}",
        " no shutdown",
        "exit",

        # ── Default route ─────────────────────────────────────────────────────
        f"ip route 0.0.0.0 0.0.0.0 {gateway}",

        # ── SSH ───────────────────────────────────────────────────────────────
        "ip ssh version 2",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 3",

        # ── VTY — SSH only ────────────────────────────────────────────────────
        "line vty 0 4",
        " login local",
        " transport input ssh",
        " exec-timeout 30 0",
        "exit",

        # ── Disable plain HTTP (RESTCONF uses HTTPS on 443) ───────────────────
        "no ip http server",

        # ── Model-driven programmability ──────────────────────────────────────
        "netconf-yang",
        "restconf",
    ]

    try:
        cli.configurep(config)
        log("configurep() completed.")
    except Exception as e:
        log(f"[ERROR] configurep() failed: {e}")
        raise


def save_config():
    """Persist running config to startup config."""
    log("Saving config (write memory)...")
    try:
        output = cli.execute("write memory")
        log(f"Save: {output.strip()}")
    except Exception as e:
        log(f"[WARN] write memory failed: {e}")


# ── Verification ──────────────────────────────────────────────────────────────

def verify(device):
    """
    Post-config verification checks. Logs pass/fail per item.
    Does not abort — config is already saved at this point.
    Returns True if all checks pass.
    """
    log("--- Post-config verification ---")
    passed = 0
    failed = 0

    def check(label, command, expected):
        nonlocal passed, failed
        try:
            output = cli.execute(command)
            if expected.lower() in output.lower():
                log(f"[OK]   {label}")
                passed += 1
            else:
                log(f"[FAIL] {label} — '{expected}' not found")
                failed += 1
        except Exception as e:
            log(f"[FAIL] {label} — exception: {e}")
            failed += 1

    check("Hostname",      "show version",                      device["hostname"])
    check("Management IP", f"show interface {MGMT_INTERFACE}", device["mgmt_ip"])
    check("Default route", "show ip route 0.0.0.0",            device["gateway"])
    check("SSH enabled",   "show ip ssh",                      "SSH Enabled")
    check("NETCONF-YANG",  "show netconf-yang status",         "enabled")
    check("RESTCONF",      "show restconf",                    "enabled")

    log(f"--- {passed} passed, {failed} failed ---")
    return failed == 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("ZTP bootstrap started")
    log(f"TFTP: 10.199.64.134  |  Domain: {DOMAIN_NAME}")
    log("=" * 60)

    # 1. Read DHCP-assigned IP from the management interface
    log(f"Reading DHCP IP from {MGMT_INTERFACE}...")
    dhcp_ip = get_dhcp_ip()

    if not dhcp_ip:
        log(f"[ERROR] No IP found on {MGMT_INTERFACE}.")
        log("The device must receive a DHCP lease before ZTP can identify it.")
        sys.exit(1)

    log(f"DHCP IP: {dhcp_ip}")

    # 2. Derive device config from IP — no MAC inventory required
    log("Deriving device config from IP address scheme...")
    device = derive_device_config(dhcp_ip)

    if not device:
        log(f"[ERROR] Could not derive config from IP {dhcp_ip}.")
        log("Verify DHCP is assigning IPs from the correct management subnets.")
        sys.exit(1)

    log(f"Identified: {device['hostname']}  (rack {device['rack']}, {device['side']})")
    log(f"Management: {device['mgmt_ip']}/{device['mgmt_prefix']}  gateway {device['gateway']}")

    # 3. RSA key — required for SSH v2, checked before generating
    generate_rsa_key()

    # 4. Push bootstrap config
    try:
        apply_config(device)
    except Exception as e:
        log(f"[ERROR] Config push failed: {e}")
        sys.exit(1)

    # 5. Save
    save_config()

    # 6. Verify
    success = verify(device)

    log("=" * 60)
    if success:
        log(f"ZTP complete — {device['hostname']} ready")
        log(f"Reachable at {device['mgmt_ip']} via SSH / NETCONF / RESTCONF")
    else:
        log(f"ZTP completed with warnings — review {LOG_PATH}")
    log("=" * 60)


if __name__ == "__main__":
    main()
