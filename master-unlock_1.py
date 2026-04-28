#!/usr/bin/env python3
import re
import os
import sys
import time
import shutil
import subprocess
from datetime import datetime
import pexpect

# =========================================================
# CONFIG - PXE58
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")
PUBKEY_FILE = os.path.join(BASE_DIR, "id_rsa.pub")

SSH_PASSWORD = "$pl3nd1D"

PXE_USER = "qsitoan"
PXE_IP = "192.168.202.58"
PXE_BASE_HOME = "/home/qsitoan"
PXE_PASSWORD = "QSI@qmf54321"

FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

TFTPBOOT_DIRECTORY = "/tftpboot/pxelinux.cfg"
MOS_CUST_IMAGE = "/tftpboot/pxelinux.cfg/t6t_MOS"
MOS_IMAGE = "firmware/t6t/gp/Image_rsa_mos.img"

PXE_BOOT_IP = "10.0.3.254"

REQUIRED_FILES = [
    "cert_0.der",
    "cert_1.der",
    "cert_2.der",
    "cert_3.der",
    "token.bin",
]

GP_PROMPTS = [
    r"root@localhost:/tmp/.*#",
    r"root@localhost:/.*#",
    r"root@localhost:.*#",
    r"root@localhost#",
    r"#\s*$",
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
def today_folder_name():
    return datetime.now().strftime("%B%d") + "_unlock"

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

def get_mac_from_file(filepath):
    with open(filepath, "r") as f:
        return f.read().strip()

def find_ip(mac):
    output = run(f"{FIND_IP} {mac}")
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    return m.group(1) if m else None

def pxe_ssh(cmd):
    remote_cmd = f"cd {PXE_BASE_HOME} && {cmd}"
    full_cmd = (
        f"sshpass -p '{PXE_PASSWORD}' ssh "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"{PXE_USER}@{PXE_IP} \"{remote_cmd}\""
    )
    return run(full_cmd)

def conv_mac_format(line):
    if "MacAddress:" not in line:
        return None

    extr_mac = line.split("MacAddress:")[1].strip()
    return "01-" + extr_mac.replace(":", "-").lower()

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

        while True:
            line = proc.stdout.readline()

            if line:
                line = line.rstrip()
                full_output += line + "\n"
                print(line)

            elif proc.poll() is not None:
                break

            else:
                if time.time() - start > timeout:
                    proc.kill()
                    print_fail(f"RSCM command timeout after {timeout}s")
                    return False, full_output

                time.sleep(0.2)

        proc.wait()
        return "Completion Code: Success" in full_output, full_output

    except Exception as e:
        print_fail(f"RSCM SSH failed: {e}")
        return False, ""

def exec_slot_cmd(ip, slot, extra_cmd, timeout=90):
    cmd = f"set system cmd -i {slot} -c {extra_cmd}"
    return exec_rm_cmd(ip, cmd, timeout=timeout)

def exec_cmd(ip, slot, action, extra=None, timeout=90):
    if action == "reset":
        cmd = f"set system reset -i {slot}"
    elif action == "vreset":
        cmd = f"set system vreset -i {slot}"
    elif action == "power_on":
        cmd = f"set system on -i {slot}"
    elif action == "gp_info":
        cmd = f"show system info -i {slot} -b 1"
    elif action == "boot_mode":
        cmd = f"set system boot -i {slot} -b 1 -t {extra}"
    else:
        cmd = f"set system cmd -i {slot} -c {extra}"

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

def get_off_server_slots(rm_manager_info_output):
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
            power_state = parts[1]
            port_state = parts[2]
            port_type = parts[3]
            completion_code = parts[6]

            if port_type == "Server" and completion_code == "Success":
                if power_state.lower() == "off" or port_state.lower() == "off":
                    off_slots.append(slot)

        except Exception:
            continue

    return off_slots

def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None

def get_gp_sn_from_fru(ip, slot):
    print_step(f"Slot {slot}: get GP CARD SN from fru print 2")

    success, output = exec_slot_cmd(ip, slot, "fru print 2", timeout=60)

    if not success:
        print_fail(f"Slot {slot}: failed to run fru print 2")
        return None

    gp_sn = extract_board_serial(output)

    if gp_sn:
        print_ok(f"Slot {slot}: GP CARD SN = {gp_sn}")
        return gp_sn

    print_fail(f"Slot {slot}: cannot parse Board Serial")
    return None

# =========================================================
# PXE BOOT HELPERS
# =========================================================
def check_custom_bootimage(directory, filename):
    fullpath = os.path.join(directory, filename)

    if os.path.isfile(fullpath):
        print_info(f"Boot file exists: {fullpath}")

        with open(fullpath, "r") as f:
            content = f.read()

        if MOS_IMAGE in content:
            print_ok(f"MOS image already present in {filename}")
        else:
            print_warn("MOS image not found, replacing from template")
            shutil.copy(MOS_CUST_IMAGE, fullpath)
            print_ok(f"Updated {fullpath}")

    else:
        print_warn(f"Boot file not found: {fullpath}")
        shutil.copy(MOS_CUST_IMAGE, fullpath)
        print_ok(f"Created {fullpath}")

def prepare_pxe_boot_for_slot(rm_ip, slot):
    print_step(f"Slot {slot}: prepare PXE MOS boot")

    success, output = exec_cmd(rm_ip, slot, "gp_info", timeout=60)

    if not success:
        print_fail(f"Slot {slot}: failed to get GP MAC info")
        return False

    boot_names = []

    for line in output.splitlines():
        if "MacAddress:" in line:
            boot_name = conv_mac_format(line)
            if boot_name:
                boot_names.append(boot_name)

    if not boot_names:
        print_fail(f"Slot {slot}: no GP MAC found")
        return False

    for boot_name in boot_names:
        try:
            check_custom_bootimage(TFTPBOOT_DIRECTORY, boot_name)
        except Exception as e:
            print_fail(f"Slot {slot}: failed preparing boot file {boot_name}: {e}")
            return False

    success, _ = exec_cmd(rm_ip, slot, "boot_mode", "2", timeout=60)

    if not success:
        print_fail(f"Slot {slot}: failed to set PXE boot")
        return False

    success, _ = exec_cmd(rm_ip, slot, "reset", timeout=60)

    if not success:
        print_fail(f"Slot {slot}: failed to reset")
        return False

    print_ok(f"Slot {slot}: PXE boot + reset done")
    return True

# =========================================================
# GP CONSOLE HELPERS
# =========================================================
def gp_login(rm_ip, slot, max_wait=7):
    print_step(f"Slot {slot}: login GP CARD 8295")

    try:
        child = pexpect.spawn(
            f"sshpass -p '{SSH_PASSWORD}' ssh "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"root@{rm_ip}",
            encoding="utf-8",
            timeout=15
        )

        child.delaybeforesend = 0.2
        child.expect(r"#", timeout=15)

        child.sendline(f"start serial session -i {slot} -p 8295")

        deadline = time.time() + max_wait
        collected = ""

        while time.time() < deadline:
            child.sendline("")

            try:
                idx = child.expect(
                    GP_PROMPTS + [
                        r"login:",
                        r"[Pp]assword:",
                        r"Completion Code:\s*Failure",
                        r"Unable to",
                        r"Not found",
                        r"Connection closed",
                        pexpect.TIMEOUT,
                        pexpect.EOF,
                    ],
                    timeout=1
                )

                if child.before:
                    txt = child.before.strip()
                    if txt:
                        collected += txt + "\n"
                        print(txt)

                if idx in [0, 1, 2, 3, 4]:
                    print_ok(f"Slot {slot}: entered GP console")
                    return child

                print_fail(f"Slot {slot}: 8295 returned error or login prompt")
                child.close(force=True)
                return None

            except pexpect.TIMEOUT:
                continue

        print_fail(f"Slot {slot}: GP console not ready within {max_wait}s")

        if collected.strip():
            print_warn("Last output:")
            print(collected.strip())

        child.close(force=True)
        return None

    except Exception as e:
        print_fail(f"Slot {slot}: cannot login GP 8295: {e}")
        return None

def gp_exit(child):
    if not child:
        return

    try:
        print_step("Exiting GP console")
        child.send("~.")
        child.expect(pexpect.EOF, timeout=10)
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

            if time.time() - start > timeout:
                print_fail(f"Command timeout after {timeout}s: {cmd}")
                return False, collected

        except pexpect.EOF:
            print_fail(f"Session closed while running: {cmd}")
            return False, collected

def gp_disable_security_and_inject_key(child, public_key):
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
            return False

    ok, output = gp_send_cmd(
        child,
        f"ping -c 1 -W 1 {PXE_BOOT_IP}",
        timeout=20,
        step_desc="Ping PXE boot IP"
    )

    if ok and (("1 packets received" in output) or ("1 received" in output)):
        print_ok("GP ping PXE boot IP successful")
    else:
        print_warn("GP ping PXE boot IP failed")

    return True

def gp_check_folder_exists(child, gp_sn):
    ok, output = gp_send_cmd(
        child,
        f"test -d /tmp/{gp_sn} && echo EXISTS || echo MISSING",
        timeout=15,
        step_desc=f"Check /tmp/{gp_sn}"
    )

    return ok and "EXISTS" in output

def gp_check_required_files(child, gp_sn):
    ok, output = gp_send_cmd(
        child,
        f"cd /tmp/{gp_sn} && ls",
        timeout=20,
        step_desc=f"Check required files in /tmp/{gp_sn}"
    )

    if not ok:
        return [], REQUIRED_FILES[:]

    present = []
    missing = []

    for fname in REQUIRED_FILES:
        if re.search(rf"(^|\s){re.escape(fname)}($|\s)", output):
            present.append(fname)
        else:
            missing.append(fname)

    if present:
        print_ok(f"Found files: {', '.join(present)}")

    if missing:
        print_warn(f"Missing files: {', '.join(missing)}")

    return present, missing

def gp_generate_files(child, gp_sn, public_key):
    print_step(f"Generate CSR/token/certs for {gp_sn}")

    if not gp_disable_security_and_inject_key(child, public_key):
        print_fail("Failed to disable security / inject key")
        return False

    cmds = [
        f"mkdir -p /tmp/{gp_sn}",
        f"cd /tmp/{gp_sn}",
        f"cerberus_utility exportcsr {gp_sn}.CSR",
        "ovb_lock token token.bin",
        "cerberus_utility getcertchain 0",
    ]

    for cmd in cmds:
        ok, output = gp_send_cmd(child, cmd, timeout=60)
        if not ok:
            return False

        if "error" in output.lower():
            print_warn(f"Command output contains error: {cmd}")

    present, missing = gp_check_required_files(child, gp_sn)

    if missing:
        print_fail(f"Still missing files after generate: {', '.join(missing)}")
        return False

    return True

def gp_run_interactive_password_cmd(child, cmd, password, timeout=180, desc="interactive command"):
    print_step(desc)
    print_info(f"GP INTERACTIVE CMD: {cmd}")

    child.sendline(cmd)

    start = time.time()
    collected = ""

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
                child.sendline(password)

            elif idx in [3, 4, 5, 6, 7]:
                return True, collected

            elif idx == 8:
                print_fail(f"{desc}: session closed")
                return False, collected

        except pexpect.TIMEOUT:
            continue

def gp_prepare_remote_pxe_folder(remote_dir):
    day_folder = os.path.basename(remote_dir)

    pxe_ssh(f"mkdir -p '{day_folder}'")

    verify = pxe_ssh(f"test -d '{remote_dir}' && echo OK || echo FAIL")

    if "OK" in verify:
        print_ok(f"PXE folder ready: {remote_dir}")
        return True

    print_fail(f"Failed to create PXE folder: {remote_dir}")
    return False

def gp_scp_folder_to_pxe(child, gp_sn, remote_dir):
    src_folder = f"/tmp/{gp_sn}"

    cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-r {src_folder} {PXE_USER}@{PXE_IP}:{remote_dir}"
    )

    ok, output = gp_run_interactive_password_cmd(
        child,
        cmd,
        PXE_PASSWORD,
        timeout=180,
        desc=f"SCP {src_folder} to PXE58"
    )

    if not ok:
        return False

    verify = pxe_ssh(f"test -d '{remote_dir}/{gp_sn}' && echo OK || echo FAIL")

    if "OK" in verify:
        print_ok(f"SCP completed: {remote_dir}/{gp_sn}")
        return True

    print_fail(f"SCP verify failed for {gp_sn}")
    if output:
        print(output)

    return False

# =========================================================
# SLOT WORKFLOW
# =========================================================
def process_ready_gp_slot(rm_ip, slot, gp_sn, public_key, remote_dir):
    child = None

    try:
        child = gp_login(rm_ip, slot, max_wait=7)

        if not child:
            return False, "login_failed"

        folder_exists = gp_check_folder_exists(child, gp_sn)

        if not folder_exists:
            print_warn(f"Slot {slot}: /tmp/{gp_sn} missing, will generate files")
            if not gp_generate_files(child, gp_sn, public_key):
                return False, "generate_failed"
        else:
            print_ok(f"Slot {slot}: found /tmp/{gp_sn}")

            present, missing = gp_check_required_files(child, gp_sn)

            if missing:
                print_warn(f"Slot {slot}: missing files, will generate")
                if not gp_generate_files(child, gp_sn, public_key):
                    return False, "generate_failed"

        if not gp_scp_folder_to_pxe(child, gp_sn, remote_dir):
            return False, "scp_failed"

        return True, "success"

    finally:
        gp_exit(child)

# =========================================================
# MAIN
# =========================================================
def main():
    failed_slots = []
    copied_successfully = []
    reset_retry_slots = []

    if not os.path.isfile(MAC_FILE):
        print_fail(f"Cannot find MAC file: {MAC_FILE}")
        sys.exit(1)

    if not os.path.isfile(PUBKEY_FILE):
        print_fail(f"Cannot find public key file: {PUBKEY_FILE}")
        sys.exit(1)

    print_step("Read RM MAC")
    rm_mac = get_mac_from_file(MAC_FILE)

    if not rm_mac:
        print_fail("RM_MAC.txt is empty")
        sys.exit(1)

    print_ok(f"RM MAC = {rm_mac}")

    print_step("Find RM IP")
    rm_ip = find_ip(rm_mac)

    if not rm_ip:
        print_fail(f"Cannot find RM IP for MAC {rm_mac}")
        sys.exit(1)

    print_ok(f"RM IP = {rm_ip}")

    print_step("Run show manager info")
    ok, rm_manager_info = exec_rm_cmd(rm_ip, RSCM_SHOW_MANAGER_INFO, timeout=90)

    if not ok:
        print_fail("Failed to get show manager info")
        sys.exit(1)

    print("\n========== SHOW MANAGER INFO OUTPUT ==========")
    print(rm_manager_info)
    print("==============================================")

    slots = get_server_slots(rm_manager_info)
    off_slots = get_off_server_slots(rm_manager_info)

    if off_slots:
        print_info(f"OFF slots found: {off_slots}")

        for slot in off_slots:
            print_step(f"Power on slot {slot}")
            exec_cmd(rm_ip, slot, "power_on", timeout=60)
    else:
        print_ok("No OFF slots found")

    if not slots:
        print_fail("No valid server slots found")
        sys.exit(1)

    print_ok(f"Server slots to process: {slots}")

    with open(PUBKEY_FILE, "r") as f:
        public_key = f.read().strip()

    day_folder = today_folder_name()
    remote_dir = f"{PXE_BASE_HOME}/{day_folder}"

    print_info(f"PXE destination folder: {remote_dir}")

    if not gp_prepare_remote_pxe_folder(remote_dir):
        sys.exit(1)

    gp_sn_map = {}

    print("\n========== MAIN WORKFLOW: TRY LOGIN GP FIRST ==========")

    for slot in slots:
        print("\n" + "=" * 70)
        print_step(f"START SLOT {slot}")

        gp_sn = get_gp_sn_from_fru(rm_ip, slot)

        if not gp_sn:
            failed_slots.append((slot, "cannot get GP SN"))
            continue

        gp_sn_map[slot] = gp_sn

        ok, reason = process_ready_gp_slot(
            rm_ip,
            slot,
            gp_sn,
            public_key,
            remote_dir
        )

        if ok:
            copied_successfully.append((slot, gp_sn))
            continue

        if reason == "login_failed":
            print_warn(f"Slot {slot}: GP login failed, prepare PXE MOS boot then retry later")

            prepared = prepare_pxe_boot_for_slot(rm_ip, slot)

            if prepared:
                reset_retry_slots.append(slot)
            else:
                failed_slots.append((slot, "login failed and PXE boot prepare failed"))

        else:
            failed_slots.append((slot, reason))

    if reset_retry_slots:
        print("\n========== RETRY SLOTS AFTER PXE BOOT RESET ==========")
        print_info(f"Retry slots: {reset_retry_slots}")

        for slot in reset_retry_slots:
            print("\n" + "=" * 70)
            print_step(f"RETRY SLOT {slot}")

            gp_sn = gp_sn_map.get(slot)

            if not gp_sn:
                gp_sn = get_gp_sn_from_fru(rm_ip, slot)

            if not gp_sn:
                failed_slots.append((slot, "retry cannot get GP SN"))
                continue

            ok, reason = process_ready_gp_slot(
                rm_ip,
                slot,
                gp_sn,
                public_key,
                remote_dir
            )

            if ok:
                copied_successfully.append((slot, gp_sn))
            else:
                failed_slots.append((slot, f"retry failed: {reason}"))

    else:
        print_ok("No retry slots needed")

    print("\n" + "=" * 70)
    print("[SUMMARY] DONE")
    print("=" * 70)

    print("\nCopied successfully:")
    if copied_successfully:
        for slot, gp_sn in copied_successfully:
            print(f"  - Slot {slot}: {gp_sn}")
    else:
        print("  None")

    print("\nFailed / manual check:")
    if failed_slots:
        for slot, reason in failed_slots:
            print(f"  - Slot {slot}: {reason}")
    else:
        print("  None")

    print(f"\nPXE target folder: {remote_dir}")


if __name__ == "__main__":
    main()
