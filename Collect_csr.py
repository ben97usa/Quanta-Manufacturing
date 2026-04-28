#!/usr/bin/env python3
import re
import subprocess
import sys
import time
import os
import shutil
import datetime
import pexpect

MAC_FILE = "./RM_MAC.txt"
SSH_PASSWORD = "$pl3nd1D"
FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

TFTPBOOT_DIRECTORY = "/tftpboot/pxelinux.cfg"
MOS_CUST_IMAGE = "/tftpboot/pxelinux.cfg/t6t_MOS"
MOS_IMAGE = "firmware/t6t/gp/Image_rsa_mos.img"

PUBKEY_FILE = "/home/qsitoan/project/UNLOCK_GP/id_rsa.pub"

PXE_BOOT_IP = "10.0.3.254"
SCP_IP = "192.168.202.50"
SCP_USER = "qsitoan"
SCP_PASSWORD = "QSI@qmf54321"

CSR_PATH = "/home/RMA_GPCARD/CSR"

GP_PROMPT = r"root@localhost:.*#"


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


def conv_mac_format(line):
    if "MacAddress:" not in line:
        return None
    extr_mac = line.split("MacAddress:")[1].strip()
    return "01-" + extr_mac.replace(":", "-").lower()


def check_custom_bootimage(directory, filename):
    fullpath = os.path.join(directory, filename)

    if os.path.isfile(fullpath):
        print(f"[INFO] Boot file exists: {fullpath}")

        with open(fullpath, "r") as f:
            content = f.read()

        if MOS_IMAGE in content:
            print(f"[OK] MOS image already present in {filename}")
        else:
            print("[WARN] MOS image not found, replacing from template")
            shutil.copy(MOS_CUST_IMAGE, fullpath)
            print(f"[OK] Updated {fullpath}")
    else:
        print(f"[WARN] Boot file not found: {fullpath}")
        shutil.copy(MOS_CUST_IMAGE, fullpath)
        print(f"[OK] Created {fullpath}")


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


def exec_cmd(ip, slot, action, extra=None):
    if action == "vreset":
        cmd = f"set system vreset -i {slot}"
    elif action == "reset":
        cmd = f"set system reset -i {slot}"
    elif action == "gp_info":
        cmd = f"show system info -i {slot} -b 1"
    elif action == "boot_mode":
        cmd = f"set system boot -i {slot} -b 1 -t {extra}"
    elif action == "power_on":
        cmd = f"set system on -i {slot}"
    else:
        cmd = f"set system cmd -i {slot} -c {extra}"

    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"root@{ip}",
        cmd
    ]

    print(f"[CMD] {cmd}")

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        full_output = ""

        for line in proc.stdout:
            line = line.rstrip()
            full_output += line + "\n"

            if "Completion Code:" in line:
                print(line)

            if "MacAddress:" in line:
                print(line)

        proc.wait()

    except Exception as e:
        print(f"[FAIL] SSH execution failed: {e}")
        return False, ""

    return "Completion Code: Success" in full_output, full_output


def exec_rm_cmd(ip, cmd):
    ssh_cmd = [
        "sshpass", "-p", SSH_PASSWORD, "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"root@{ip}",
        cmd
    ]

    print(f"[CMD] {cmd}")

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        full_output = ""

        for line in proc.stdout:
            line = line.rstrip()
            full_output += line + "\n"

        proc.wait()

    except Exception as e:
        print(f"[FAIL] RM SSH execution failed: {e}")
        return False, ""

    return "Completion Code: Success" in full_output, full_output


def extract_board_serial(output):
    for line in output.splitlines():
        if "Board Serial" in line:
            return line.split(":", 1)[1].strip()
    return None


def gp_login(ip, slot):
    try:
        child = pexpect.spawn(
            f"sshpass -p '{SSH_PASSWORD}' ssh "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"root@{ip}",
            encoding="utf-8",
            timeout=15
        )

        child.logfile = sys.stdout

        child.expect(r"#", timeout=15)

        child.sendline(f"start serial session -i {slot} -p 8295")

        end_time = time.time() + 7

        while time.time() < end_time:
            child.sendline("")

            idx = child.expect(
                [
                    GP_PROMPT,
                    pexpect.TIMEOUT,
                    pexpect.EOF
                ],
                timeout=1
            )

            if idx == 0:
                print(f"[OK] Slot {slot}: GP console ready")
                return child

            if idx == 2:
                print(f"[FAIL] Slot {slot}: GP console EOF")
                child.close()
                return None

        print(f"[FAIL] Slot {slot}: GP console not ready within 7 seconds")
        child.close()
        return None

    except Exception as e:
        print(f"[FAIL] Cannot enter GP console slot {slot}: {e}")
        return None


def gp_exit(child):
    if not child:
        return

    try:
        print("[STEP] Exiting GP/RSCM session")
        child.send("~.")
        child.expect(pexpect.EOF, timeout=10)
        child.close()
    except Exception as e:
        print(f"[WARN] Exit session issue: {e}")


def gp_ping_ok(child, ip):
    try:
        child.sendline(f"ping -c 1 -W 1 {ip}")
        child.expect(GP_PROMPT, timeout=20)
        output = child.before
        return ("1 packets received" in output) or ("1 received" in output)
    except Exception:
        return False


def gp_disable_firewall(child, public_key):
    print("[STEP] Disable firewall / inject public key")

    cmds = [
        "setenforce 0",
        "mkdir -p /run/ssh/keys/root",
        f'printf "%s\\n" "{public_key}" > /run/ssh/keys/root/authorized_keys',
        "chmod 644 /run/ssh/keys/root/*",
        "ov-firewall --disable"
    ]

    for cmd in cmds:
        print(f"[GP CMD] {cmd}")
        child.sendline(cmd)
        child.expect(GP_PROMPT, timeout=60)

        output = child.before.splitlines()[1:]
        if output:
            print("\n".join(output))

    if gp_ping_ok(child, PXE_BOOT_IP):
        print("[OK] GP ping PXE boot IP successful")
    else:
        print("[WARN] GP ping PXE boot IP failed")


def gp_collect_keys(child, gp_sn):
    print(f"[STEP] Generate CSR/token/certs for {gp_sn}")

    cmds = [
        f"mkdir -p /tmp/{gp_sn}",
        f"cd /tmp/{gp_sn}",
        f"cerberus_utility exportcsr {gp_sn}.CSR",
        "ovb_lock token token.bin",
        "cerberus_utility getcertchain 0"
    ]

    for cmd in cmds:
        print(f"[GP CMD] {cmd}")
        child.sendline(cmd)
        child.expect(GP_PROMPT, timeout=60)

        output = child.before.splitlines()[1:]
        if output:
            print("\n".join(output))


def gp_transfer_csr_to_pxe(child, csr_file, folder_path):
    print(f"[STEP] Check CSR file: {csr_file}")

    child.sendline(f"ls {csr_file}")
    child.expect(GP_PROMPT, timeout=30)

    check_output = child.before

    if "No such file" in check_output or "cannot access" in check_output:
        print(f"[FAIL] CSR file not found on GP: {csr_file}")
        return False

    cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"{csr_file} {SCP_USER}@{SCP_IP}:{folder_path}/"
    )

    print(f"[CMD] {cmd}")
    child.sendline(cmd)

    while True:
        idx = child.expect(
            [
                r"yes/no",
                r"[Pp]assword:",
                GP_PROMPT,
                pexpect.TIMEOUT,
                pexpect.EOF
            ],
            timeout=30
        )

        if idx == 0:
            child.sendline("yes")

        elif idx == 1:
            child.sendline(SCP_PASSWORD)

        elif idx == 2:
            print("[OK] SCP command finished")

            filename = os.path.basename(csr_file)
            verify_path = os.path.join(folder_path, filename)

            verify = run(f"test -f '{verify_path}' && echo OK || echo FAIL").strip()

            if verify == "OK":
                print(f"[OK] Verified CSR on PXE: {verify_path}")
                return True

            print(f"[FAIL] CSR not found on PXE after SCP: {verify_path}")
            return False

        else:
            print("[FAIL] SCP CSR failed or timeout")
            return False


def create_today_csr_folder():
    now = datetime.datetime.now()
    folder_name = f"{now.strftime('%B')}{now.day}_CSRs"
    folder_path = os.path.join(CSR_PATH, folder_name)

    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        print(f"[OK] Created CSR folder: {folder_path}")
    else:
        print(f"[OK] CSR folder already exists: {folder_path}")

    return folder_path


def process_slot(ip, server, public_key, folder_path):
    gp_child = None

    try:
        print(f"\n========== Processing slot {server} ==========")

        print(f"[STEP] Slot {server}: get GP serial")
        success, gp_fru_output = exec_cmd(ip, server, "cmd", "fru print 2")

        if not success:
            print(f"[FAIL] Slot {server}: cannot get FRU")
            return False, None, "cannot get FRU"

        gp_sn = extract_board_serial(gp_fru_output)

        if not gp_sn:
            print(f"[FAIL] Slot {server}: cannot extract GP_SN")
            return False, None, "cannot extract GP_SN"

        print(f"[OK] Slot {server}: GP_SN = {gp_sn}")

        gp_child = gp_login(ip, server)

        if not gp_child:
            print(f"[WARN] Slot {server}: cannot login GP")
            return False, gp_sn, "cannot login GP"

        gp_disable_firewall(gp_child, public_key)
        gp_collect_keys(gp_child, gp_sn)

        csr_file = f"/tmp/{gp_sn}/{gp_sn}.CSR"

        scp_ok = gp_transfer_csr_to_pxe(gp_child, csr_file, folder_path)

        if not scp_ok:
            print(f"[FAIL] Slot {server}: SCP CSR failed")
            return False, gp_sn, "SCP CSR failed"

        print(f"[OK] Slot {server}: CSR collected successfully")
        return True, gp_sn, "success"

    except Exception as e:
        print(f"[FAIL] Slot {server}: unexpected error: {e}")
        return False, None, f"unexpected error: {e}"

    finally:
        gp_exit(gp_child)


def print_summary(total_slots, ready_servers, collected_csrs, failed_csr_slots, failed_list):
    failed_csr_slots = sorted(set(failed_csr_slots))
    failed_list = sorted(set(failed_list))

    print("\n========== CSR COLLECTION SUMMARY ==========")
    print(f"Total server slots found: {total_slots}")
    print(f"Total ready slots after boot/reset: {len(ready_servers)}")
    print(f"Collected CSRs: {len(collected_csrs)}")
    print(f"Failed to collect CSR: {len(failed_csr_slots)}")
    print(f"Failed CSR slots: {failed_csr_slots}")

    print("\nCollected CSR detail:")
    if collected_csrs:
        for slot, sn in collected_csrs:
            print(f"  Slot {slot}: {sn}.CSR")
    else:
        print("  None")

    print("\nAll failed/problem slots:")
    print(f"  {failed_list}")
    print("============================================")


def main():
    failed_list = []
    failed_csr_slots = []
    collected_csrs = []

    print("[STEP] Read RM MAC")
    rm_mac = get_mac_from_file(MAC_FILE)

    if not rm_mac:
        print("[FAIL] RM_MAC.txt is empty")
        return

    print(f"[OK] RM MAC = {rm_mac}")

    print("[STEP] Find RM IP")
    ip = find_ip(rm_mac)

    if not ip:
        print(f"[FAIL] Cannot find RM IP for MAC {rm_mac}")
        return

    print(f"[OK] RM IP = {ip}")

    print("[STEP] Get RM manager info")
    cmd_succeed, rm_manager_info = exec_rm_cmd(ip, RSCM_SHOW_MANAGER_INFO)

    if not cmd_succeed:
        print(f"[FAIL] Failed to get manager info from RM {rm_mac} / {ip}")
        return

    print("\n========== SHOW MANAGER INFO OUTPUT ==========")
    print(rm_manager_info)
    print("==============================================")

    print("[STEP] Load public key")
    try:
        with open(PUBKEY_FILE, "r") as f:
            public_key = f.read().strip()
    except Exception as e:
        print(f"[FAIL] Cannot read public key file: {PUBKEY_FILE}")
        print(e)
        return

    folder_path = create_today_csr_folder()

    slots = get_server_slots(rm_manager_info)
    off_slots = get_off_server_slots(rm_manager_info)

    total_slots = len(slots)

    print(f"[OK] Server slots at {rm_mac} ({ip}): {slots}")

    if off_slots:
        print(f"[INFO] Off server slots found: {off_slots}")

        for slot in off_slots:
            print(f"[STEP] Slot {slot}: power on")
            exec_cmd(ip, slot, "power_on", None)
    else:
        print("[OK] No off server slots found")

    if not slots:
        print("[FAIL] No valid server slots found")
        return

    server_list = []
    boot_images = []

    print("\n========== STEP 1: Collect GP MAC info ==========")

    for slot in slots:
        print(f"\n=== Slot {slot}: get GP info ===")

        success, output = exec_cmd(ip, slot, "gp_info", None)

        if not success:
            print(f"[FAIL] Slot {slot}: failed to get GP info")
            failed_list.append(slot)
            failed_csr_slots.append(slot)
            continue

        found_mac = False

        for line in output.splitlines():
            if "MacAddress:" in line:
                boot_name = conv_mac_format(line)

                if boot_name:
                    boot_images.append(boot_name)
                    found_mac = True

        if not found_mac:
            print(f"[FAIL] Slot {slot}: no GP MAC found")
            failed_list.append(slot)
            failed_csr_slots.append(slot)
            continue

        server_list.append(slot)
        print(f"[OK] Slot {slot}: GP info collected")

    print("\n========== STEP 2: Prepare PXE boot files ==========")

    for bootimage in boot_images:
        try:
            check_custom_bootimage(TFTPBOOT_DIRECTORY, bootimage)
        except Exception as e:
            print(f"[FAIL] Boot image prepare failed: {bootimage} | {e}")

    print("\n========== STEP 3: Set PXE boot + reset ==========")

    ready_servers = []

    for server in server_list:
        print(f"\n=== Slot {server}: set boot PXE + reset ===")

        success, _ = exec_cmd(ip, server, "boot_mode", "2")

        if not success:
            print(f"[FAIL] Slot {server}: failed to set PXE boot")
            failed_list.append(server)
            failed_csr_slots.append(server)
            continue

        success, _ = exec_cmd(ip, server, "reset", None)

        if not success:
            print(f"[FAIL] Slot {server}: failed to reset")
            failed_list.append(server)
            failed_csr_slots.append(server)
            continue

        ready_servers.append(server)
        print(f"[OK] Slot {server}: ready")

    print("\n========== STEP 4: Login GP + Generate CSR + SCP ==========")

    retry_slots = []

    for server in ready_servers:
        ok, gp_sn, reason = process_slot(ip, server, public_key, folder_path)

        if ok:
            collected_csrs.append((server, gp_sn))
        else:
            if reason == "cannot login GP":
                print(f"[WARN] Slot {server}: add to retry list")
                retry_slots.append(server)
            else:
                failed_list.append(server)
                failed_csr_slots.append(server)

    if retry_slots:
        print("\n========== STEP 5: RETRY GP LOGIN FAILED SLOTS ==========")
        print(f"[INFO] Retry slots: {retry_slots}")

        for server in retry_slots:
            print(f"\n========== Retry slot {server} ==========")

            ok, gp_sn, reason = process_slot(ip, server, public_key, folder_path)

            if ok:
                collected_csrs.append((server, gp_sn))
            else:
                print(f"[FAIL] Slot {server}: still failed after retry, skip")
                failed_list.append(server)
                failed_csr_slots.append(server)
    else:
        print("\n[OK] No GP login retry needed")

    print_summary(
        total_slots=total_slots,
        ready_servers=ready_servers,
        collected_csrs=collected_csrs,
        failed_csr_slots=failed_csr_slots,
        failed_list=failed_list
    )


if __name__ == "__main__":
    main()
