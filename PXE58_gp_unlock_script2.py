#!/usr/bin/env python3
import os
import re
import sys
import time
import shutil
import subprocess
import pexpect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAC_FILE = os.path.join(BASE_DIR, "RM_MAC.txt")

SSH_PASSWORD = "$pl3nd1D"
FIND_IP = "/usr/local/bin/find_ip"
RSCM_SHOW_MANAGER_INFO = "show manager info"

PXE_USER = "qsitoan"
PXE_SCP_IP = "192.168.202.58"
PXE_PASSWORD = "QSI@qmf54321"

SIGNED_TOKEN_BASE = "/home/qsitoan/signed_token"

TFTPBOOT_DIRECTORY = "/tftpboot/pxelinux.cfg"
MOS_TEMPLATE = "/tftpboot/pxelinux.cfg/t6t_MOS"
MOS_EXPECTED_MD5 = "19f5811deb3468d19d9fa8d5e4aad275"

GP_REBOOT_MAX_WAIT = 120
GP_REBOOT_INTERVAL = 5


GP_PROMPTS = [
    r"root@localhost:/tmp/.*#",
    r"root@localhost:/.*#",
    r"root@localhost:.*#",
    r"root@localhost#",
    r"#\s*$",
]

def safe_print(msg=""):
    print(msg, flush=True)


def print_step(msg):
    safe_print(f"\n[STEP] {msg}")


def print_info(msg):
    safe_print(f"[INFO] {msg}")


def print_ok(msg):
    safe_print(f"[OK] {msg}")


def print_warn(msg):
    safe_print(f"[WARN] {msg}")


def print_fail(msg):
    safe_print(f"[FAIL] {msg}")


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


def parse_policy_value(output):
    patterns = [
        r"Policy\s*Retrieved\.\s*Policy\s*=\s*(0x[0-9a-fA-F]+)",
        r"Retrieved\s*Policy\s*=\s*(0x[0-9a-fA-F]+)",
        r"Policy\s*=\s*(0x[0-9a-fA-F]+)",
    ]

    for p in patterns:
        m = re.search(p, output, re.IGNORECASE)
        if m:
            return m.group(1).lower()

    return None

def get_queue_name_from_path():
    m = re.search(r"UNLOCK_GP_(Q\d+)", BASE_DIR)
    if m:
        return m.group(1)
    return "Q?"


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
                safe_print(line)

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
        print_ok(f"Created/updated: {dst}")
        return True
    except Exception as e:
        print_fail(f"Failed to create {dst}: {e}")
        return False


def remove_custom_boot_image(slot, boot_file):
    print_step(f"Slot {slot}: Remove custom boot image {boot_file}")

    if not boot_file:
        print_warn(f"Slot {slot}: empty boot file, skip remove")
        return False

    path = os.path.join(TFTPBOOT_DIRECTORY, boot_file)

    if not os.path.isfile(path):
        print_warn(f"Slot {slot}: custom boot image not found: {path}")
        return True

    try:
        os.remove(path)
        print_ok(f"Removed: {path}")
        return True
    except Exception as e:
        print_fail(f"Failed to remove {path}: {e}")
        return False


def remove_all_custom_boot_images(slot, boot_files):
    ok_all = True

    for boot_file in boot_files:
        if not remove_custom_boot_image(slot, boot_file):
            ok_all = False

    return ok_all


def gp_login(rm_ip, slot, max_wait=120, quiet=False):
    if not quiet:
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

                if child.before and not quiet:
                    txt = child.before.strip()
                    if txt:
                        collected += txt + "\n"
                        safe_print(txt)

                if idx < len(GP_PROMPTS):
                    if not quiet:
                        print_ok(f"Slot {slot}: entered GP console")
                    return child

                if not quiet:
                    print_fail(f"Slot {slot}: GP login returned error")
                child.close(force=True)
                return None

            except pexpect.TIMEOUT:
                if not quiet:
                    print_info(f"Slot {slot}: still waiting GP prompt...")
                child.sendline("")

        if not quiet:
            print_fail(f"Slot {slot}: GP console not ready within {max_wait}s")

            if collected.strip():
                print_warn("Last GP output:")
                safe_print(collected.strip())

        child.close(force=True)
        return None

    except Exception as e:
        if not quiet:
            print_fail(f"Slot {slot}: cannot login GP: {e}")
        return None


def gp_exit(child):
    if not child:
        return

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


def wait_gp_ready_after_reboot(rm_ip, slot, max_wait=GP_REBOOT_MAX_WAIT, interval=GP_REBOOT_INTERVAL):
    print_step(f"Slot {slot}: Wait GP ready after reboot")

    start = time.time()

    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)
        print_info(f"Slot {slot}: probing GP console... {elapsed}s")

        child = gp_login(rm_ip, slot, max_wait=10, quiet=True)

        if child:
            print_ok(f"Slot {slot}: GP ready after {elapsed}s")
            gp_exit(child)
            return True

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
    return wait_gp_ready_after_reboot(rm_ip, slot)


def prepare_mos_boot(rm_ip, slot):
    print_step(f"Slot {slot}: Prepare MOS boot")

    if not set_soc_bootmode_2(rm_ip, slot):
        return False, []

    boot_files = get_slot_pxe_boot_files(rm_ip, slot)

    if not boot_files:
        return False, []

    for boot_file in boot_files:
        if not create_custom_boot_image(slot, boot_file):
            return False, boot_files

    if not reboot_slot(rm_ip, slot):
        return False, boot_files

    return True, boot_files


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
                    safe_print(txt)

            return True, collected

        except pexpect.TIMEOUT:
            if child.before:
                txt = child.before.strip()
                if txt:
                    collected += txt + "\n"
                    safe_print(txt)

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


def gp_disable_firewall(child):
    print_step("Disable firewall")

    cmds = [
        "setenforce 0",
        "ov-firewall --disable",
    ]

    for cmd in cmds:
        ok, _ = gp_send_cmd(child, cmd, timeout=60)
        if not ok:
            print_fail("Failed to disable firewall")
            return False

    return True


def gp_check_bootmode_2(child, slot):
    print_step(f"Slot {slot}: Check GP bootmode")

    ok, output = gp_send_cmd(
        child,
        "cerberus_utility fpgagetbootmode",
        timeout=30
    )

    if not ok:
        print_fail(f"Slot {slot}: cannot check GP bootmode")
        return False

    if re.search(r"\b2\b", output):
        print_ok(f"Slot {slot}: GP bootmode is 2")
        return True

    print_warn(f"Slot {slot}: GP bootmode is NOT 2")
    return False


def gp_force_bootmode_2(child, slot):
    print_step(f"Slot {slot}: Force GP bootmode to 2")

    ok, _ = gp_send_cmd(
        child,
        "cerberus_utility fpgasetbootmode 2",
        timeout=30
    )

    if not ok:
        print_fail(f"Slot {slot}: failed to set GP bootmode 2")
        return False

    time.sleep(2)
    return gp_check_bootmode_2(child, slot)


def gp_make_sure_bootmode_2(child, slot):
    if gp_check_bootmode_2(child, slot):
        return True

    return gp_force_bootmode_2(child, slot)


def gp_get_policy(child):
    ok, output = gp_send_cmd(
        child,
        "ovb_lock policy get /tmp/policy.bin",
        timeout=30,
        step_desc="Check unlock policy"
    )

    if not ok:
        return None, output

    policy = parse_policy_value(output)

    if policy:
        return policy, output

    return "unknown", output


def gp_prepare_tmp_signed_token(child):
    ok, _ = gp_send_cmd(
        child,
        "rm -f /tmp/signed_token.bin",
        timeout=15,
        step_desc="Remove old /tmp/signed_token.bin"
    )

    return ok


def gp_scp_signed_token(child, gp_sn):
    print_step(f"SCP signed_token.bin to GP for {gp_sn}")

    remote_file = f"{SIGNED_TOKEN_BASE}/{gp_sn}/signed_token.bin"

    if not os.path.isfile(remote_file):
        print_fail(f"signed_token.bin not found on PXE: {remote_file}")
        return False

    scp_cmd = (
        f"scp -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"{PXE_USER}@{PXE_SCP_IP}:{remote_file} /tmp/signed_token.bin"
    )

    print_info(f"GP SCP CMD: {scp_cmd}")
    child.sendline(scp_cmd)

    start = time.time()
    password_sent = False

    while True:
        if time.time() - start > 180:
            print_fail("SCP signed_token timeout")
            return False

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
                    safe_print(txt)

            if idx in [0, 1]:
                child.sendline("yes")

            elif idx == 2:
                if password_sent:
                    print_fail("SCP failed: password asked again")
                    return False
                child.sendline(PXE_PASSWORD)
                password_sent = True

            elif idx == 3:
                print_fail("SCP failed: permission denied")
                return False

            elif idx == 4:
                print_fail("SCP failed: no such file")
                return False

            elif idx == 5:
                continue

            elif 6 <= idx < 6 + len(GP_PROMPTS):
                break

            elif idx == 6 + len(GP_PROMPTS):
                print_fail("SCP session closed")
                return False

            else:
                continue

        except pexpect.TIMEOUT:
            continue

    ok, output = gp_send_cmd(
        child,
        "test -f /tmp/signed_token.bin && echo EXISTS || echo MISSING",
        timeout=15,
        step_desc="Verify /tmp/signed_token.bin"
    )

    if ok and "EXISTS" in output:
        print_ok("/tmp/signed_token.bin exists")
        return True

    print_fail("/tmp/signed_token.bin missing after SCP")
    return False


def gp_apply_signed_token(child):
    ok, output = gp_send_cmd(
        child,
        "ovb_lock policy set /tmp/signed_token.bin",
        timeout=60,
        step_desc="Apply signed_token.bin"
    )

    if not ok:
        print_fail("ovb_lock policy set failed")
        return False, output

    return True, output


POST_COMMANDS = [
    ("yes | sudo mkfs.ext4 /dev/mmcblk0p5", 300),
    ("mount /dev/mmcblk0p5 /vol/data", 60),
    ("e2label /dev/mmcblk0p5 DATA", 30),
]


def run_post_commands_on_slot(child, slot):
    results = []

    for cmd, timeout in POST_COMMANDS:
        print_step(f"Slot {slot}: run post command")
        ok, output = gp_send_cmd(child, cmd, timeout=timeout)

        if ok:
            print_ok(f"Slot {slot}: post command finished")
        else:
            print_fail(f"Slot {slot}: post command failed, continue next command")

        results.append((cmd, ok))

    return results


def try_login_or_prepare_mos(rm_ip, slot):
    print_step(f"Slot {slot}: Try login first")

    child = gp_login(rm_ip, slot, max_wait=25)

    boot_files = get_slot_pxe_boot_files(rm_ip, slot)

    if child:
        print_ok(f"Slot {slot}: GP already reachable, continue unlock")
        return child, boot_files

    print_warn(f"Slot {slot}: GP not reachable, prepare MOS boot")

    ok, boot_files = prepare_mos_boot(rm_ip, slot)

    if not ok:
        return None, boot_files

    child = gp_login(rm_ip, slot, max_wait=120)

    return child, boot_files


def final_reboot_and_policy_check(rm_ip, slot):
    if not reboot_slot(rm_ip, slot):
        return None

    child = gp_login(rm_ip, slot, max_wait=60)

    if not child:
        print_fail(f"Slot {slot}: cannot login after final reboot")
        return None

    try:
        gp_disable_firewall(child)

        policy, raw = gp_get_policy(child)
        print_info(f"Final policy after reboot = {policy}")

        if raw and raw.strip():
            safe_print(raw.strip())

        return policy

    finally:
        gp_exit(child)


def process_slot(rm_ip, slot):
    child = None
    boot_files = []

    print("\n" + "=" * 70)
    print_step(f"START SLOT {slot}")

    gp_sn = get_gp_sn_from_fru(rm_ip, slot)

    if not gp_sn:
        return False, "cannot_get_gp_sn", None

    token_path = f"{SIGNED_TOKEN_BASE}/{gp_sn}/signed_token.bin"

    if not os.path.isfile(token_path):
        print_warn(f"Slot {slot}: no signed_token.bin found: {token_path}")
        return False, "no_signed_token", gp_sn

    child, boot_files = try_login_or_prepare_mos(rm_ip, slot)

    if not child:
        return False, "cannot_login_even_after_mos_boot", gp_sn

    try:
        if not gp_disable_firewall(child):
            return False, "disable_firewall_failed", gp_sn

        policy1, raw1 = gp_get_policy(child)
        print_info(f"Current policy = {policy1}")

        if raw1 and raw1.strip():
            safe_print(raw1.strip())

        if policy1 == "0x2":
            print_ok(f"Slot {slot}: already unlocked")

            run_post_commands_on_slot(child, slot)

            remove_all_custom_boot_images(slot, boot_files)

            gp_make_sure_bootmode_2(child, slot)

            gp_exit(child)
            child = None

            policy_final = final_reboot_and_policy_check(rm_ip, slot)

            if policy_final == "0x2":
                return True, "already_unlocked_final_ok", gp_sn

            return False, "already_unlocked_but_final_check_failed", gp_sn

        if not gp_prepare_tmp_signed_token(child):
            return False, "prepare_tmp_failed", gp_sn

        if not gp_scp_signed_token(child, gp_sn):
            return False, "scp_signed_token_failed", gp_sn

        applied, apply_output = gp_apply_signed_token(child)

        if apply_output and apply_output.strip():
            safe_print(apply_output.strip())

        if not applied:
            return False, "policy_set_failed", gp_sn

        policy2, raw2 = gp_get_policy(child)
        print_info(f"Policy after unlock = {policy2}")

        if raw2 and raw2.strip():
            safe_print(raw2.strip())

        if policy2 != "0x2":
            return False, "policy_not_0x2_after_unlock", gp_sn

        print_ok(f"Slot {slot}: UNLOCK SUCCESS")

        run_post_commands_on_slot(child, slot)

        remove_all_custom_boot_images(slot, boot_files)

        gp_make_sure_bootmode_2(child, slot)

        gp_exit(child)
        child = None

        policy_final = final_reboot_and_policy_check(rm_ip, slot)

        if policy_final == "0x2":
            print_ok(f"Slot {slot}: final policy still unlocked after reboot")
            return True, "unlock_successfully", gp_sn

        print_fail(f"Slot {slot}: final policy check failed")
        return False, "final_policy_check_failed", gp_sn

    finally:
        if child:
            gp_exit(child)


def main():
    print_step("Check required files")

    if not os.path.isfile(MAC_FILE):
        print_fail(f"RM_MAC.txt not found: {MAC_FILE}")
        sys.exit(1)

    if not verify_mos_md5():
        print_fail("STOP: t6t_MOS checksum is wrong")
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
        print_fail(f"Cannot find RM IP from MAC {rm_mac}")
        sys.exit(1)

    print_ok(f"RM IP = {rm_ip}")

    print_step("Get rack info")
    ok, rm_manager_info = exec_rm_cmd(rm_ip, RSCM_SHOW_MANAGER_INFO, timeout=90)

    if not ok:
        print_fail("Failed to get show manager info")
        sys.exit(1)

    slots = get_server_slots(rm_manager_info)

    if not slots:
        print_fail("No valid server slots found")
        sys.exit(1)

    print_ok(f"Server slots to process: {slots}")

    success_list = []
    failed_list = []
    no_token_list = []

    for slot in slots:
        try:
            ok, reason, gp_sn = process_slot(rm_ip, slot)

            if ok:
                success_list.append((slot, gp_sn, reason))
            else:
                if reason == "no_signed_token":
                    no_token_list.append((slot, gp_sn))
                else:
                    failed_list.append((slot, gp_sn if gp_sn else "UNKNOWN", reason))

        except KeyboardInterrupt:
            print_fail("User interrupted script")
            sys.exit(1)

        except Exception as e:
            print_fail(f"Unexpected error at slot {slot}: {e}")
            failed_list.append((slot, "UNKNOWN", f"unexpected_error: {e}"))
            continue

    success_list.sort(key=lambda x: x[0])
    failed_list.sort(key=lambda x: x[0])
    no_token_list.sort(key=lambda x: x[0])

    print("\n" + "=" * 70)
    print("[SUMMARY] SCRIPT 2 UNLOCK DONE")
    print("=" * 70)

    queue_name = get_queue_name_from_path()

    print("\nMessage to send:")
    print(f"{queue_name} unlocked successfully {len(success_list)} units. Please check them out; they are ready for testing. Then check in new units to continue unlocking.")

    print("\nUnlock success:")
    if success_list:
        for slot, gp_sn, reason in success_list:
            if reason.startswith("already"):
                print(f"  - Slot {slot}: {gp_sn} (already unlocked)")
            else:
                print(f"  - Slot {slot}: {gp_sn}")
    else:
        print("  None")

    print(f"\nTotal success: {len(success_list)}")

    print("\nNo signed_token.bin:")
    if no_token_list:
        for slot, gp_sn in no_token_list:
            print(f"  - Slot {slot}: {gp_sn}")
    else:
        print("  None")
    print("\nFailed / manual check:")
    if failed_list:
        for slot, gp_sn, reason in failed_list:
            print(f"  - Slot {slot}: {gp_sn} - {reason}")
    else:
        print("  None")

    print("\nSigned token source path:")
    print(f"  {SIGNED_TOKEN_BASE}/GPCARDSN/signed_token.bin")


if __name__ == "__main__":
    main()
