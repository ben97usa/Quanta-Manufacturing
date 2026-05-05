#!/usr/bin/env python3
import os
import re
import sys
import time
import shutil
import subprocess
import pexpect
from datetime import datetime

# =========================================================
# CONFIG
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")
PUBKEY_FILE = os.path.join(BASE_DIR, "id_rsa.pub")

SSH_PASSWORD = "$pl3nd1D"
FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

PXE_USER = "qsitoan"
PXE_SCP_IP = "192.168.202.58"      # change to 192.168.202.50 / .54 if needed
PXE_PASSWORD = "QSI@qmf54321"

PXE_BOOT_IP = "10.0.3.254"

TFTPBOOT_DIRECTORY = "/tftpboot/pxelinux.cfg"
MOS_TEMPLATE = "/tftpboot/pxelinux.cfg/t6t_MOS"
MOS_EXPECTED_MD5 = "19f5811deb3468d19d9fa8d5e4aad275"

CSR_BASE_PATH = "/home/RMA_GPCARD/CSR"
UNSIGNED_TOKEN_BASE = "/home/qsitoan/unsigned_token"

REQUIRED_FILES = [
    "token.bin",
    "cert_0.der",
    "cert_1.der",
    "cert_2.der",
    "cert_3.der",
]

GP_PROMPTS = [
    r"root@localhost:/tmp/.*#",
    r"root@localhost:/.*#",
    r"root@localhost:.*#",
    r"root@localhost#",
]

# =========================================================
# PRINT HELPERS
# =========================================================
def print_step(msg):
    print(f"\n[STEP] {msg}")

def print_info(msg):
    print(f"[INFO] {msg}")

def print_ok(msg):
    print(f"[OK] {msg}")

def print_warn(msg):
    print(f"[WARN] {msg}")

def print_fail(msg):
    print(f"[FAIL] {msg}")

# =========================================================
# BASIC HELPERS
# =========================================================
def run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            shell=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        return e.output if e.output else ""

def today_csr_folder_name():
    return datetime.now().strftime("%B%d") + "_CSRs"

def get_today_csr_path():
    return os.path.join(CSR_BASE_PATH, today_csr_folder_name())

def get_mac_from_file(filepath):
    with open(filepath, "r") as f:
        return f.read().strip()

def find_ip(mac):
    output = run(f"{FIND_IP} {mac}")
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    return m.group(1) if m else None

def verify_mos_md5():
    print_step("Verify t6t_MOS checksum")

    if not os.path.isfile(MOS_TEMPLATE):
        print_fail(f"MOS file not found: {MOS_TEMPLATE}")
        return False

    output = run(f"md5sum {MOS_TEMPLATE}")
    print_info(f"md5sum output: {output.strip()}")

    if MOS_EXPECTED_MD5 in output:
        print_ok("MOS checksum matched")
        return True

    print_fail("MOS checksum mismatch")
    print_fail(f"Expected: {MOS_EXPECTED_MD5}")
    return False

def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None

# =========================================================
# RSCM HELPERS
# =========================================================
def exec_rm_cmd(ip, cmd, timeout=90):
    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"root@{ip}",
        cmd
    ]

    print_info(f"RSCM CMD: {cmd}")

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        full_output = ""
        start = time.time()
        last_status = time.time()

        while True:
            line = proc.stdout.readline()

            if line:
                line = line.rstrip()
                full_output += line + "\n"
                print(line)

            elif proc.poll() is not None:
                break

            else:
                now = time.time()

                if now - start > timeout:
                    proc.kill()
                    print_fail(f"RSCM timeout after {timeout}s")
                    return False, full_output

                if now - last_status > 15:
                    print_warn(f"Still waiting on RSCM... {int(now - start)}s")
                    last_status = now

                time.sleep(0.2)

        proc.wait()
        return "Completion Code: Success" in full_output, full_output

    except Exception as e:
        print_fail(f"RSCM SSH failed: {e}")
        return False, ""

def exec_slot_cmd(ip, slot, extra_cmd, timeout=60):
    cmd = f"set system cmd -i {slot} -c {extra_cmd}"
    return exec_rm_cmd(ip, cmd, timeout=timeout)

def get_server_slots(rm_manager_info_output):
    server_slots = []

    for line in rm_manager_info_output.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue
        if "Port State" in line or "----" in line:
            continue

        parts = [p.strip() for p in line.split("|") if p.strip()]

        if len(parts) < 7:
            continue

        try:
            slot = int(parts[0])
            port_type = parts[3]
            completion_code = parts[6]

            if port_type == "Server" and completion_code == "Success":
                server_slots.append(slot)

        except Exception:
            continue

    return server_slots

def get_off_slots(rm_manager_info_output):
    off_slots = []

    for line in rm_manager_info_output.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue
        if "Port State" in line or "----" in line:
            continue

        parts = [p.strip() for p in line.split("|") if p.strip()]

        if len(parts) < 7:
            continue

        try:
            slot = int(parts[0])
            power_state = parts[1].lower()
            port_state = parts[2].lower()
            port_type = parts[3]
            completion_code = parts[6]

            if port_type == "Server" and completion_code == "Success":
                if power_state == "off" or port_state == "off":
                    off_slots.append(slot)

        except Exception:
            continue

    return off_slots

def power_on_off_slots(rm_ip, rm_manager_info_output):
    off_slots = get_off_slots(rm_manager_info_output)

    if not off_slots:
        print_ok("No OFF slots found")
        return

    print_warn(f"OFF slots found: {off_slots}")

    for slot in off_slots:
        print_step(f"Power on slot {slot}")
        exec_rm_cmd(rm_ip, f"set system on -i {slot}", timeout=60)

def get_gp_sn_from_fru(rm_ip, slot):
    print_step(f"Slot {slot}: Get GP CARD SN")

    ok, output = exec_slot_cmd(rm_ip, slot, "fru print 2", timeout=60)

    if not ok:
        print_fail(f"Slot {slot}: cannot run fru print 2")
        return None

    gp_sn = extract_board_serial(output)

    if gp_sn:
        print_ok(f"Slot {slot}: GP CARD SN = {gp_sn}")
        return gp_sn

    print_fail(f"Slot {slot}: cannot parse GP CARD SN")
    return None

def get_slot_pxe_boot_files(rm_ip, slot):
    print_step(f"Slot {slot}: Get PXE boot file name")

    ok, output = exec_rm_cmd(rm_ip, f"show system info -i {slot} -b 1", timeout=60)

    if not ok:
        print_fail(f"Slot {slot}: cannot get system info")
        return []

    boot_files = []

    for line in output.splitlines():
        m = re.search(r"MacAddress\s*:\s*([0-9a-fA-F:]{17})", line)
        if m:
            mac = m.group(1).lower()
            boot_file = "01-" + mac.replace(":", "-")
            boot_files.append(boot_file)

    if boot_files:
        print_ok(f"Slot {slot}: boot files = {boot_files}")
    else:
        print_fail(f"Slot {slot}: cannot parse MacAddress")

    return boot_files

def create_custom_boot_image(slot, boot_file):
    print_step(f"Slot {slot}: Create custom boot image {boot_file}")

    if not boot_file:
        print_fail(f"Slot {slot}: empty boot file")
        return False

    dst = os.path.join(TFTPBOOT_DIRECTORY, boot_file)

    if not os.path.isfile(MOS_TEMPLATE):
        print_fail(f"MOS template missing: {MOS_TEMPLATE}")
        return False

    try:
        shutil.copy(MOS_TEMPLATE, dst)
        print_ok(f"Created: {dst}")
        return True
    except Exception as e:
        print_fail(f"Failed to create {dst}: {e}")
        return False

def set_soc_bootmode_2(rm_ip, slot):
    print_step(f"Slot {slot}: Change SOC Bootmode to 2")

    ok, _ = exec_rm_cmd(
        rm_ip,
        f"set system boot -i {slot} -b 1 -t 2",
        timeout=60
    )

    if ok:
        print_ok(f"Slot {slot}: SOC bootmode set to 2")
        return True

    print_fail(f"Slot {slot}: failed to set SOC bootmode 2")
    return False

def wait_for_gp_ready(rm_ip, slot, max_wait=180, interval=10):
    print_step(f"Slot {slot}: Wait for GP ready after reboot")

    start = time.time()

    while time.time() - start < max_wait:
        print_info(f"Slot {slot}: checking GP console readiness... {int(time.time() - start)}s")

        child = gp_login(rm_ip, slot, max_wait=12)

        if child:
            print_ok(f"Slot {slot}: GP is ready")
            gp_exit(child)
            return True

        print_info(f"Slot {slot}: GP not ready yet, wait {interval}s")
        time.sleep(interval)

    print_fail(f"Slot {slot}: GP not ready after {max_wait}s")
    return False


def reboot_slot(rm_ip, slot):
    print_step(f"Slot {slot}: Reboot GP card")

    ok, _ = exec_rm_cmd(
        rm_ip,
        f"set system reset -i {slot}",
        timeout=60
    )

    if not ok:
        print_fail(f"Slot {slot}: reboot failed")
        return False

    print_ok(f"Slot {slot}: reboot triggered")

    return wait_for_gp_ready(rm_ip, slot, max_wait=180, interval=10)

def prepare_mos_boot(rm_ip, slot):
    print_step(f"Slot {slot}: Prepare MOS boot")

    if not set_soc_bootmode_2(rm_ip, slot):
        return False

    boot_files = get_slot_pxe_boot_files(rm_ip, slot)

    if not boot_files:
        return False

    for boot_file in boot_files:
        if not create_custom_boot_image(slot, boot_file):
            return False

    if not reboot_slot(rm_ip, slot):
        return False

    return True

# =========================================================
# GP CONSOLE HELPERS
# =========================================================
def gp_login(rm_ip, slot, max_wait=120):
    print_step(f"Slot {slot}: Login GP CARD 8295")

    try:
        child = pexpect.spawn(
            f"sshpass -p '{SSH_PASSWORD}' ssh -tt "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"root@{rm_ip}",
            encoding="utf-8",
            timeout=30
        )

        child.delaybeforesend = 0.2
        child.expect([r"RScmCli#", r"#"], timeout=30)

        child.sendline(f"start serial session -i {slot} -p 8295")

        deadline = time.time() + max_wait
        collected = ""

        while time.time() < deadline:
            try:
                idx = child.expect(
                    GP_PROMPTS + [
                        r"login:",
                        r"[Pp]assword:",
                        r"Completion Code:\s*Failure",
                        r"Unable to",
                        r"Not found",
                        r"Connection closed",
                    ],
                    timeout=8
                )

                if child.before:
                    txt = child.before.strip()
                    if txt:
                        collected += txt + "\n"
                        print(txt)

                if idx < len(GP_PROMPTS):
                    print_ok(f"Slot {slot}: entered GP console")
                    return child

                print_fail(f"Slot {slot}: GP login returned error")
                child.close(force=True)
                return None

            except pexpect.TIMEOUT:
                print_info(f"Slot {slot}: still waiting GP prompt...")
                child.sendline("")

        print_fail(f"Slot {slot}: GP console not ready within {max_wait}s")

        if collected.strip():
            print_warn("Last GP output:")
            print(collected.strip())

        child.close(force=True)
        return None

    except Exception as e:
        print_fail(f"Slot {slot}: cannot login GP: {e}")
        return None

def gp_exit(child):
    if not child:
        return

    print_step("Exit GP console")

    try:
        child.sendcontrol("d")
        time.sleep(1)
    except Exception:
        pass

    try:
        child.send("~.")
        child.expect(pexpect.EOF, timeout=5)
    except Exception:
        pass

    try:
        child.close(force=True)
    except Exception:
        pass

def gp_send_cmd(child, cmd, timeout=30, step_desc=None):
    if step_desc:
        print_step(step_desc)

    print_info(f"GP CMD: {cmd}")
    child.sendline(cmd)

    start = time.time()
    last_status = time.time()
    collected = ""

    while True:
        try:
            child.expect(GP_PROMPTS, timeout=5)

            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            return True, collected

        except pexpect.TIMEOUT:
            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            now = time.time()

            if now - start > timeout:
                print_fail(f"Command timeout after {timeout}s: {cmd}")
                return False, collected

            if now - last_status > 10:
                print_warn(f"Still waiting command... {int(now - start)}s")
                last_status = now

        except pexpect.EOF:
            print_fail(f"Session closed while running: {cmd}")
            return False, collected

def gp_disable_firewall(child, public_key):
    print_step("Disable firewall and inject public key")

    cmds = [
        "setenforce 0",
        "mkdir -p /run/ssh/keys/root",
        f'printf "%s\\n" "{public_key}" > /run/ssh/keys/root/authorized_keys',
        "chmod 644 /run/ssh/keys/root/authorized_keys",
        "ov-firewall --disable",
    ]

    for cmd in cmds:
        ok, _ = gp_send_cmd(child, cmd, timeout=60)
        if not ok:
            print_fail("Failed during firewall/key setup")
            return False

    ok, output = gp_send_cmd(
        child,
        f"ping -c 1 -W 1 {PXE_BOOT_IP}",
        timeout=20,
        step_desc="Ping PXE boot IP"
    )

    if ok and ("1 packets received" in output or "1 received" in output):
        print_ok("GP ping PXE boot IP success")
    else:
        print_warn("GP ping PXE boot IP failed, continue anyway")

    return True

# =========================================================
# CSR / TOKEN / CERT GENERATION
# =========================================================
def gp_check_folder_exists(child, gp_sn):
    ok, output = gp_send_cmd(
        child,
        f"test -d /tmp/{gp_sn} && echo EXISTS || echo MISSING",
        timeout=15,
        step_desc=f"Check /tmp/{gp_sn}"
    )

    return ok and "EXISTS" in output

def gp_check_required_files(child, gp_sn):
    csr_file = f"{gp_sn}.CSR"

    expected_files = [csr_file] + REQUIRED_FILES

    ok, output = gp_send_cmd(
        child,
        f"cd /tmp/{gp_sn} && ls",
        timeout=20,
        step_desc=f"Check generated files in /tmp/{gp_sn}"
    )

    if not ok:
        return [], expected_files

    present = []
    missing = []

    for fname in expected_files:
        if re.search(rf"(^|\s){re.escape(fname)}($|\s)", output):
            present.append(fname)
        else:
            missing.append(fname)

    if present:
        print_ok(f"Present: {', '.join(present)}")

    if missing:
        print_warn(f"Missing: {', '.join(missing)}")

    return present, missing

def gp_generate_csr_token_certchain(child, gp_sn):
    print_step(f"Generate CSR / token / certchain for {gp_sn}")

    cmds = [
        f"mkdir -p /tmp/{gp_sn}",
        f"cd /tmp/{gp_sn}",
        f"cerberus_utility exportcsr {gp_sn}.CSR",
        "ovb_lock token token.bin",
        "cerberus_utility getcertchain 0",
        "sync",
    ]

    for cmd in cmds:
        ok, output = gp_send_cmd(child, cmd, timeout=90)

        if not ok:
            print_fail(f"Generate command failed: {cmd}")
            return False

        if "error" in output.lower() or "failed" in output.lower():
            print_warn(f"Command output may contain error: {cmd}")

    present, missing = gp_check_required_files(child, gp_sn)

    if missing:
        print_fail(f"Still missing files after generate: {', '.join(missing)}")
        return False

    print_ok(f"{gp_sn}: CSR/token/certchain ready")
    return True

def gp_make_sure_files_ready(child, gp_sn):
    folder_exists = gp_check_folder_exists(child, gp_sn)

    if folder_exists:
        print_ok(f"/tmp/{gp_sn} already exists")
        present, missing = gp_check_required_files(child, gp_sn)

        if not missing:
            print_ok(f"{gp_sn}: all files already exist")
            return True

        print_warn(f"{gp_sn}: missing files, regenerate")

    else:
        print_warn(f"/tmp/{gp_sn} missing, generate new")

    return gp_generate_csr_token_certchain(child, gp_sn)

# =========================================================
# SCP HELPERS
# =========================================================
def gp_run_interactive_scp(child, cmd, timeout=180, desc="SCP"):
    print_step(desc)
    print_info(f"GP SCP CMD: {cmd}")

    child.sendline(cmd)

    start = time.time()
    collected = ""
    password_sent = False

    while True:
        if time.time() - start > timeout:
            print_fail(f"{desc}: timeout after {timeout}s")
            return False, collected

        try:
            idx = child.expect(
                [
                    r"Are you sure you want to continue connecting \(yes/no(/\[fingerprint\])?\)\?",
                    r"yes/no",
                    r"[Pp]assword:",
                    r"Permission denied",
                    r"No such file or directory",
                    r"100%",
                ] + GP_PROMPTS + [
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=8
            )

            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    print(txt)

            if idx in [0, 1]:
                child.sendline("yes")

            elif idx == 2:
                if password_sent:
                    print_fail(f"{desc}: password asked again")
                    return False, collected
                child.sendline(PXE_PASSWORD)
                password_sent = True

            elif idx == 3:
                print_fail(f"{desc}: permission denied")
                return False, collected

            elif idx == 4:
                print_fail(f"{desc}: no such file or directory")
                return False, collected

            elif idx == 5:
                continue

            elif 6 <= idx < 6 + len(GP_PROMPTS):
                print_ok(f"{desc}: completed")
                return True, collected

            elif idx == 6 + len(GP_PROMPTS):
                print_fail(f"{desc}: session closed")
                return False, collected

            else:
                continue

        except pexpect.TIMEOUT:
            continue

def scp_csr_only(child, gp_sn, csr_dest):
    print_step(f"SCP CSR only for {gp_sn}")

    gp_csr = f"/tmp/{gp_sn}/{gp_sn}.CSR"

    os.makedirs(csr_dest, exist_ok=True)

    cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"{gp_csr} {PXE_USER}@{PXE_SCP_IP}:{csr_dest}/"
    )

    ok, _ = gp_run_interactive_scp(
        child,
        cmd,
        timeout=180,
        desc=f"SCP CSR only {gp_sn}"
    )

    if not ok:
        return False

    local_file = os.path.join(csr_dest, f"{gp_sn}.CSR")

    if os.path.isfile(local_file):
        print_ok(f"CSR copied to PXE: {local_file}")
        return True

    print_warn(f"Cannot verify local CSR file: {local_file}")
    return True

def scp_whole_gp_folder(child, gp_sn):
    print_step(f"SCP whole GP folder for {gp_sn}")

    os.makedirs(UNSIGNED_TOKEN_BASE, exist_ok=True)

    src = f"/tmp/{gp_sn}"
    dst = UNSIGNED_TOKEN_BASE

    cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-r {src} {PXE_USER}@{PXE_SCP_IP}:{dst}/"
    )

    ok, _ = gp_run_interactive_scp(
        child,
        cmd,
        timeout=240,
        desc=f"SCP whole folder {gp_sn}"
    )

    if not ok:
        return False

    local_folder = os.path.join(UNSIGNED_TOKEN_BASE, gp_sn)

    if os.path.isdir(local_folder):
        print_ok(f"Whole folder copied to PXE: {local_folder}")
        return True

    print_warn(f"Cannot verify local folder: {local_folder}")
    return True

def ask_scp_mode():
    print("\nChoose SCP mode:")
    print("1 = CSR only")
    print("2 = Whole GP CARD SN folder")
    print("")
    print("Mode 1 destination:")
    print(f"  {get_today_csr_path()}")
    print("")
    print("Mode 2 destination:")
    print(f"  {UNSIGNED_TOKEN_BASE}/GPCARDSN/")
    print("")

    while True:
        choice = input("Enter 1 or 2: ").strip()

        if choice in ["1", "2"]:
            return choice

        print("Invalid choice. Please enter 1 or 2.")

# =========================================================
# SLOT PROCESS
# =========================================================
def process_slot(rm_ip, slot, public_key, scp_mode, csr_dest):
    child = None

    try:
        gp_sn = get_gp_sn_from_fru(rm_ip, slot)

        if not gp_sn:
            return False, "cannot_get_gp_sn", None

        if not prepare_mos_boot(rm_ip, slot):
            return False, "prepare_mos_boot_failed", gp_sn

        child = gp_login(rm_ip, slot, max_wait=120)

        if not child:
            return False, "gp_login_failed", gp_sn

        if not gp_disable_firewall(child, public_key):
            return False, "disable_firewall_failed", gp_sn

        if not gp_make_sure_files_ready(child, gp_sn):
            return False, "generate_files_failed", gp_sn

        if scp_mode == "1":
            if not scp_csr_only(child, gp_sn, csr_dest):
                return False, "scp_csr_failed", gp_sn
        else:
            if not scp_whole_gp_folder(child, gp_sn):
                return False, "scp_whole_folder_failed", gp_sn

        return True, "success", gp_sn

    finally:
        gp_exit(child)

# =========================================================
# MAIN
# =========================================================
def main():
    print_step("Check required files")

    if not os.path.isfile(MAC_FILE):
        print_fail(f"RM_MAC.txt not found: {MAC_FILE}")
        sys.exit(1)

    if not os.path.isfile(PUBKEY_FILE):
        print_fail(f"id_rsa.pub not found: {PUBKEY_FILE}")
        sys.exit(1)

    if not verify_mos_md5():
        print_fail("STOP: t6t_MOS checksum is wrong")
        sys.exit(1)

    csr_dest = get_today_csr_path()
    os.makedirs(csr_dest, exist_ok=True)
    os.makedirs(UNSIGNED_TOKEN_BASE, exist_ok=True)

    scp_mode = ask_scp_mode()

    print_step("Read RM MAC")
    rm_mac = get_mac_from_file(MAC_FILE)

    if not rm_mac:
        print_fail("RM_MAC.txt is empty")
        sys.exit(1)

    print_ok(f"RM MAC = {rm_mac}")

    print_step("Find RM IP")
    rm_ip = find_ip(rm_mac)

    if not rm_ip:
        print_fail(f"Cannot find RM IP from MAC {rm_mac}")
        sys.exit(1)

    print_ok(f"RM IP = {rm_ip}")

    print_step("Read public key")
    with open(PUBKEY_FILE, "r") as f:
        public_key = f.read().strip()

    if not public_key:
        print_fail("id_rsa.pub is empty")
        sys.exit(1)

    print_step("Get rack info")
    ok, rm_manager_info = exec_rm_cmd(rm_ip, RSCM_SHOW_MANAGER_INFO, timeout=90)

    if not ok:
        print_fail("Failed to get show manager info")
        sys.exit(1)

    power_on_off_slots(rm_ip, rm_manager_info)

    slots = get_server_slots(rm_manager_info)

    if not slots:
        print_fail("No valid server slots found")
        sys.exit(1)

    print_ok(f"Server slots to process: {slots}")

    success_list = []
    failed_list = []

    for slot in slots:
        print("\n" + "=" * 70)
        print_step(f"START SLOT {slot}")

        ok, reason, gp_sn = process_slot(
            rm_ip,
            slot,
            public_key,
            scp_mode,
            csr_dest
        )

        if ok:
            success_list.append((slot, gp_sn))
        else:
            failed_list.append((slot, gp_sn if gp_sn else "UNKNOWN", reason))

    print("\n" + "=" * 70)
    print("[SUMMARY] SCRIPT 1 CSR COLLECTION DONE")
    print("=" * 70)

    print("\nSuccessful:")
    if success_list:
        for slot, gp_sn in success_list:
            print(f"  - Slot {slot}: {gp_sn}")
    else:
        print("  None")

    print("\nFailed / manual check:")
    if failed_list:
        for slot, gp_sn, reason in failed_list:
            print(f"  - Slot {slot}: {gp_sn} - {reason}")
    else:
        print("  None")

    print("\nOutput paths:")
    if scp_mode == "1":
        print(f"  CSR only path: {csr_dest}")
    else:
        print(f"  Whole folder path: {UNSIGNED_TOKEN_BASE}/GPCARDSN/")


if __name__ == "__main__":
    main()
