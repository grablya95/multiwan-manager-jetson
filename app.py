#!/usr/bin/env python3
"""Multi-provider WAN failover manager for Linux."""

import atexit
import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from flask import Flask, jsonify, render_template, request


STATUS_INIT = "Ініціалізація"
STATUS_STABLE = "Стабільний"
STATUS_DEGRADED = "Деградація"
STATUS_DOWN = "Упав"

TRACK_TARGET_POOL = [
    "8.8.8.8",
    "1.1.1.1",
    "9.9.9.9",
    "208.67.222.222",
    "8.8.4.4",
    "1.0.0.1",
    "149.112.112.112",
    "208.67.220.220",
]

LEGACY_IPTABLES_CHAINS = {
    "OUTPUT": "WAN_FAILOVER_OUTPUT",
    "FORWARD": "WAN_FAILOVER_FORWARD",
}

PROFILE_PATH = Path(__file__).with_name("providers.json")
PERSISTED_CONFIG_KEYS = {
    "auto_fallback",
    "failover_on_degraded",
    "interval_seconds",
    "ping_count",
    "ping_timeout_seconds",
    "down_failures",
    "up_successes",
    "primary_recovery_successes",
    "switch_cooldown_seconds",
    "route_audit_seconds",
    "flush_conntrack",
}

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("multiwan")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("waitress").setLevel(logging.WARNING)


@dataclass
class Provider:
    identity: str
    key: str
    iface: str
    gateway: Optional[str]
    local_ip: Optional[str]
    label: str
    hardware_name: str
    hardware_type: str
    hardware_model: str
    driver: str
    bus: str
    priority: int
    link_up: bool = True
    original_metric: Optional[int] = None
    targets: List[str] = field(default_factory=list)
    ping_threshold: float = 250.0
    loss_threshold: int = 25
    required_targets: int = 1
    fail_count: int = 0
    recover_count: int = 0
    stats: Dict[str, Any] = field(
        default_factory=lambda: {
            "ping": 0.0,
            "jitter": 0.0,
            "loss": 100,
            "status": STATUS_INIT,
            "checked_targets": [],
        }
    )


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
lock = threading.RLock()
monitor_lock = threading.Lock()
stop_event = threading.Event()
cleanup_lock = threading.Lock()
cleanup_completed = False

state: Dict[str, Any] = {
    "mode": "auto",
    "active_provider": None,
    "active_interface": "unknown",
    "last_switch": 0.0,
    "last_error": "",
    "warnings": [],
    "providers": [],
    "profiles": {},
    "installed_tracking_routes": {},
    "last_route_audit": 0.0,
    "started_at": time.time(),
    "last_check_at": 0.0,
    "last_check_duration_ms": 0,
    "interface_metadata_cache": {},
    "config": {
        "auto_fallback": True,
        "failover_on_degraded": True,
        "interval_seconds": 5,
        "ping_count": 2,
        "ping_timeout_seconds": 1,
        "down_failures": 3,
        "up_successes": 2,
        "primary_recovery_successes": 4,
        "switch_cooldown_seconds": 15,
        "route_audit_seconds": 60,
        "flush_conntrack": True,
    },
}


def require_root() -> None:
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0:
        logger.error("Цьому сервісу потрібні права root.")
        sys.exit(1)


def run_cmd(args: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def run_quiet(args: List[str], timeout: int = 10) -> Optional[subprocess.CompletedProcess]:
    try:
        return run_cmd(args, timeout=timeout)
    except Exception:
        return None


def run_best_effort(args: List[str], timeout: int = 10) -> bool:
    proc = run_quiet(args, timeout=timeout)
    if not proc:
        return False
    if proc.returncode != 0 and proc.stderr:
        with lock:
            state["last_error"] = proc.stderr.strip()
    return proc.returncode == 0


def remove_legacy_iptables_chain(parent_chain: str, managed_chain: str) -> None:
    while True:
        check = run_quiet(["iptables", "-w", "-C", parent_chain, "-j", managed_chain])
        if not check or check.returncode != 0:
            break
        run_quiet(["iptables", "-w", "-D", parent_chain, "-j", managed_chain])
    run_quiet(["iptables", "-w", "-F", managed_chain])
    run_quiet(["iptables", "-w", "-X", managed_chain])


def clear_legacy_firewall_rules() -> None:
    for parent_chain, managed_chain in LEGACY_IPTABLES_CHAINS.items():
        remove_legacy_iptables_chain(parent_chain, managed_chain)


def parse_default_routes(output: str) -> List[Dict[str, Any]]:
    routes: List[Dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "default" or "dev" not in parts:
            continue

        route: Dict[str, Any] = {"gateway": None, "iface": None, "metric": 1000}
        for index, part in enumerate(parts):
            if part == "via" and index + 1 < len(parts):
                route["gateway"] = parts[index + 1]
            elif part == "dev" and index + 1 < len(parts):
                route["iface"] = parts[index + 1]
            elif part == "metric" and index + 1 < len(parts):
                try:
                    route["metric"] = int(parts[index + 1])
                except ValueError:
                    pass

        if route["iface"] and route["iface"] != "lo":
            routes.append(route)
    return sorted(routes, key=lambda item: item["metric"])


def default_routes_snapshot() -> List[Dict[str, Any]]:
    proc = run_quiet(["ip", "-4", "route", "show", "default"])
    if not proc or proc.returncode != 0:
        return []
    return parse_default_routes(proc.stdout)


def list_interfaces() -> Dict[str, Dict[str, Any]]:
    proc = run_quiet(["ip", "-j", "-4", "addr", "show"])
    if not proc or proc.returncode != 0:
        return {}

    try:
        raw_interfaces = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}

    interfaces: Dict[str, Dict[str, Any]] = {}
    for item in raw_interfaces:
        iface = item.get("ifname")
        if not iface or iface == "lo":
            continue
        mac = str(item.get("address") or "").lower()
        identity = f"mac:{mac}" if mac and mac != "00:00:00:00:00:00" else f"ifname:{iface}"
        flags = set(item.get("flags") or [])
        operstate = str(item.get("operstate") or "UNKNOWN").upper()
        local_ip = next(
            (
                address.get("local")
                for address in item.get("addr_info", [])
                if address.get("family") == "inet" and address.get("scope") == "global"
            ),
            None,
        )
        metadata_cache = state["interface_metadata_cache"]
        metadata = metadata_cache.get(identity)
        if metadata is None:
            metadata = detect_interface_hardware(iface)
            metadata_cache[identity] = metadata
        else:
            metadata = dict(metadata)
            metadata["hardware_name"] = (
                f"{metadata.get('hardware_type', 'Мережевий адаптер')} ({iface})"
            )
        interfaces[identity] = {
            "identity": identity,
            "iface": iface,
            "link_up": "LOWER_UP" in flags or operstate not in {"DOWN", "NOTPRESENT"},
            "local_ip": local_ip,
            **metadata,
        }
    return interfaces


def udev_properties(iface: str) -> Dict[str, str]:
    sysfs_path = f"/sys/class/net/{iface}"
    proc = run_quiet(
        ["udevadm", "info", "--query=property", f"--path={sysfs_path}"],
        timeout=3,
    )
    if not proc or proc.returncode != 0:
        return {}
    properties: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and value:
            properties[key] = value.replace("\\x20", " ").strip()
    return properties


def sysfs_driver(iface: str) -> str:
    driver_path = Path("/sys/class/net") / iface / "device" / "driver"
    try:
        return driver_path.resolve(strict=True).name
    except OSError:
        return ""


def detect_interface_hardware(iface: str) -> Dict[str, str]:
    properties = udev_properties(iface)
    driver = properties.get("ID_NET_DRIVER") or sysfs_driver(iface)
    bus = properties.get("ID_BUS", "")
    model = properties.get("ID_MODEL_FROM_DATABASE") or properties.get("ID_MODEL", "")
    vendor = properties.get("ID_VENDOR_FROM_DATABASE") or properties.get("ID_VENDOR", "")
    model = model.replace("_", " ")
    vendor = vendor.replace("_", " ")
    model = " ".join(part for part in (vendor, model) if part).strip()

    iface_lower = iface.lower()
    driver_lower = driver.lower()
    model_lower = model.lower()
    try:
        device_path = str((Path("/sys/class/net") / iface / "device").resolve()).lower()
    except OSError:
        device_path = ""
    is_usb = bus.lower() == "usb" or "/usb" in device_path or iface_lower.startswith("enx")
    is_mobile = (
        iface_lower.startswith(("wwan", "wwp", "rmnet", "ppp"))
        or any(token in driver_lower for token in ("qmi", "mbim", "wwan", "huawei_cdc_ncm"))
        or any(
            token in model_lower
            for token in ("lte", "5g", "4g", "modem", "quectel", "simcom", "sierra wireless")
        )
    )

    if is_mobile:
        hardware_type = "LTE / WWAN"
    elif is_usb:
        hardware_type = "USB Ethernet"
    elif iface_lower.startswith(("wl", "wlan")):
        hardware_type = "Wi-Fi"
    elif iface_lower.startswith(("eth", "en")):
        hardware_type = "Ethernet"
    else:
        hardware_type = "Мережевий адаптер"

    hardware_name = f"{hardware_type} ({iface})"
    return {
        "hardware_name": hardware_name,
        "hardware_type": hardware_type,
        "hardware_model": model,
        "driver": driver,
        "bus": bus.upper() if bus else ("USB" if is_usb else ""),
    }


def nmcli_gateway(iface: str) -> Optional[str]:
    proc = run_quiet(["nmcli", "-g", "IP4.GATEWAY", "device", "show", iface], timeout=5)
    if not proc or proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        value = line.strip()
        if value:
            return value
    return None


def provider_key(identity: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", identity).strip("_").lower()
    return f"wan_{safe or 'provider'}"


def default_profile(
    identity: str,
    iface: str,
    gateway: Optional[str],
    metric: Optional[int],
    hardware_name: str,
) -> Dict[str, Any]:
    profiles = state["profiles"]
    used_priorities = [int(item.get("priority", 0)) for item in profiles.values()]
    priority = max(used_priorities, default=-1) + 1
    target = TRACK_TARGET_POOL[priority % len(TRACK_TARGET_POOL)]
    return {
        "identity": identity,
        "key": provider_key(identity),
        "iface": iface,
        "gateway": gateway,
        "label": hardware_name,
        "priority": priority,
        "original_metric": metric,
        "targets": [target],
        "ping_threshold": 150.0 if priority == 0 else 250.0,
        "loss_threshold": 15 if priority == 0 else 25,
        "required_targets": 1,
    }


def load_profiles() -> None:
    if not PROFILE_PATH.exists():
        return
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profiles = payload.get("profiles", [])
        with lock:
            state["profiles"] = {
                str(item["identity"]): item
                for item in profiles
                if isinstance(item, dict) and item.get("identity")
            }
            saved_config = payload.get("config", {})
            if isinstance(saved_config, dict):
                for key in PERSISTED_CONFIG_KEYS:
                    if key in saved_config:
                        state["config"][key] = saved_config[key]
            saved_mode = payload.get("mode", "auto")
            if saved_mode == "auto" or (
                isinstance(saved_mode, str) and saved_mode.startswith("manual:")
            ):
                state["mode"] = saved_mode
    except Exception as exc:
        with lock:
            state["warnings"].append(f"Не вдалося прочитати providers.json: {exc}")
        logger.warning("Не вдалося прочитати providers.json: %s", exc)


def save_profiles() -> None:
    with lock:
        profiles = sorted(state["profiles"].values(), key=lambda item: int(item.get("priority", 0)))
        saved_mode = state["mode"]
        saved_config = {
            key: state["config"][key]
            for key in PERSISTED_CONFIG_KEYS
            if key in state["config"]
        }
    try:
        temp_path = PROFILE_PATH.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "mode": saved_mode,
                    "config": saved_config,
                    "profiles": profiles,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp_path.replace(PROFILE_PATH)
    except Exception as exc:
        with lock:
            state["warnings"].append(f"Не вдалося зберегти providers.json: {exc}")
        logger.error("Не вдалося зберегти providers.json: %s", exc)


def profile_to_provider(profile: Dict[str, Any], interface: Dict[str, Any]) -> Provider:
    iface = interface["iface"]
    gateway = profile.get("gateway") or nmcli_gateway(iface)
    return Provider(
        identity=str(profile["identity"]),
        key=str(profile["key"]),
        iface=iface,
        gateway=gateway,
        local_ip=interface.get("local_ip"),
        label=str(profile.get("label") or iface),
        hardware_name=str(interface.get("hardware_name") or f"Мережевий адаптер ({iface})"),
        hardware_type=str(interface.get("hardware_type") or "Мережевий адаптер"),
        hardware_model=str(interface.get("hardware_model") or ""),
        driver=str(interface.get("driver") or ""),
        bus=str(interface.get("bus") or ""),
        priority=int(profile.get("priority", 0)),
        link_up=bool(interface.get("link_up", True)),
        original_metric=profile.get("original_metric"),
        targets=list(profile.get("targets") or []),
        ping_threshold=float(profile.get("ping_threshold", 250.0)),
        loss_threshold=int(profile.get("loss_threshold", 25)),
        required_targets=int(profile.get("required_targets", 1)),
    )


def provider_by_key(key: Optional[str]) -> Optional[Provider]:
    with lock:
        return next((provider for provider in state["providers"] if provider.key == key), None)


def provider_signature(providers: List[Provider]) -> Tuple[Any, ...]:
    return tuple(
        (
            item.identity,
            item.key,
            item.iface,
            item.gateway,
            item.link_up,
            item.hardware_name,
            item.hardware_model,
            item.driver,
            item.priority,
            tuple(item.targets),
        )
        for item in providers
    )


def discover_providers() -> Tuple[bool, bool]:
    interfaces = list_interfaces()
    if not interfaces:
        with lock:
            state["last_error"] = "Не вдалося прочитати список мережевих інтерфейсів."
        return False, False

    routes = default_routes_snapshot()
    routes_by_iface = {route["iface"]: route for route in routes}

    with lock:
        profiles = state["profiles"]
        old_providers = list(state["providers"])
        old_by_identity = {item.identity: item for item in old_providers}
        old_signature = provider_signature(old_providers)

    profile_changed = False
    for identity, interface in interfaces.items():
        iface = interface["iface"]
        route = routes_by_iface.get(iface)
        if identity not in profiles:
            stale_identity = next(
                (
                    profile_identity
                    for profile_identity, profile in profiles.items()
                    if profile.get("iface") == iface and profile_identity != identity
                ),
                None,
            )
            if stale_identity:
                profile = profiles.pop(stale_identity)
                profile["identity"] = identity
                profiles[identity] = profile
                profile_changed = True
            elif route:
                profiles[identity] = default_profile(
                    identity,
                    iface,
                    route.get("gateway"),
                    route.get("metric"),
                    str(interface.get("hardware_name") or iface),
                )
                profile_changed = True

    providers: List[Provider] = []
    used_keys: Set[str] = set()
    claimed_ifaces: Set[str] = set()
    profile_items = sorted(
        profiles.items(),
        key=lambda item: 0 if item[0] in interfaces else 1,
    )
    for identity, profile in profile_items:
        interface = interfaces.get(identity)
        if not interface:
            fallback_iface = profile.get("iface")
            interface = next(
                (item for item in interfaces.values() if item["iface"] == fallback_iface),
                None,
            )
        if not interface:
            continue

        iface = interface["iface"]
        if iface in claimed_ifaces:
            continue
        claimed_ifaces.add(iface)
        route = routes_by_iface.get(iface)
        if profile.get("iface") != iface:
            profile["iface"] = iface
            profile_changed = True
        current_label = str(profile.get("label") or "")
        if re.fullmatch(
            r"(?:Ethernet|USB Ethernet|LTE / WWAN|Wi-Fi|Мережевий адаптер) \(.+\)",
            current_label,
        ) and current_label != interface.get("hardware_name"):
            profile["label"] = interface.get("hardware_name")
            profile_changed = True
        if route and route.get("gateway") and profile.get("gateway") != route["gateway"]:
            profile["gateway"] = route["gateway"]
            profile_changed = True
        if route and profile.get("original_metric") is None:
            profile["original_metric"] = route.get("metric")
            profile_changed = True

        provider = profile_to_provider(profile, interface)
        previous = old_by_identity.get(identity)
        if previous:
            provider.fail_count = previous.fail_count
            provider.recover_count = previous.recover_count
            provider.stats = previous.stats

        if provider.key in used_keys:
            provider.key = f"{provider.key}_{len(used_keys)}"
            profile["key"] = provider.key
            profile_changed = True
        used_keys.add(provider.key)
        providers.append(provider)

    identity_by_iface = {item["iface"]: identity for identity, item in interfaces.items()}
    for identity, profile in list(profiles.items()):
        iface = profile.get("iface")
        current_identity = identity_by_iface.get(iface)
        if current_identity and current_identity != identity:
            profiles.pop(identity, None)
            profile_changed = True

    providers.sort(key=lambda item: (item.priority, item.label.lower(), item.key))
    new_signature = provider_signature(providers)

    mode_changed = False
    with lock:
        state["providers"] = providers
        state["profiles"] = profiles
        if state["active_provider"] not in {item.key for item in providers}:
            state["active_provider"] = None
            state["active_interface"] = "unknown"
        if state["mode"].startswith("manual:"):
            manual_key = state["mode"].split(":", 1)[1]
            if manual_key not in {item.key for item in providers}:
                state["mode"] = "auto"
                mode_changed = True
        state["last_error"] = ""

    if profile_changed or mode_changed:
        save_profiles()
    return True, new_signature != old_signature


def validate_and_normalize_targets(providers: List[Provider]) -> Tuple[bool, List[str]]:
    used: Set[str] = set()
    warnings: List[str] = []
    changed = False
    for provider in providers:
        unique_targets: List[str] = []
        for target in provider.targets:
            try:
                ipaddress.IPv4Address(target)
            except ValueError:
                continue
            if target in used:
                warnings.append(
                    f"Track host {target} дублювався; для {provider.label} він був замінений."
                )
                changed = True
                continue
            used.add(target)
            unique_targets.append(target)

        if not unique_targets:
            fallback = next((item for item in TRACK_TARGET_POOL if item not in used), None)
            if fallback:
                used.add(fallback)
                unique_targets = [fallback]
                changed = True
            else:
                warnings.append(f"Для {provider.label} немає унікального track host.")

        if provider.targets != unique_targets:
            provider.targets = unique_targets
            profile = state["profiles"].get(provider.identity)
            if profile is not None:
                profile["targets"] = unique_targets
    return changed, warnings


def default_route_args(provider: Provider, metric: int) -> List[str]:
    args = ["ip", "-4", "route", "replace", "default"]
    if provider.gateway:
        args.extend(["via", provider.gateway])
    args.extend(["dev", provider.iface, "metric", str(metric)])
    return args


def delete_default_routes_for_provider(provider: Provider) -> None:
    for _ in range(8):
        args = ["ip", "-4", "route", "del", "default"]
        if provider.gateway:
            args.extend(["via", provider.gateway])
        args.extend(["dev", provider.iface])
        proc = run_quiet(args)
        if not proc or proc.returncode != 0:
            break


def route_args_for_target(target: str, provider: Provider) -> List[str]:
    target_net = str(ipaddress.ip_network(f"{target}/32", strict=False))
    args = ["ip", "-4", "route", "replace", target_net]
    if provider.gateway:
        args.extend(["via", provider.gateway])
    args.extend(["dev", provider.iface])
    return args


def tracking_route_matches(target: str, provider: Provider) -> bool:
    target_net = str(ipaddress.ip_network(f"{target}/32", strict=False))
    proc = run_quiet(["ip", "-4", "route", "show", target_net])
    if not proc or proc.returncode != 0:
        return False
    return f"dev {provider.iface}" in proc.stdout and (
        not provider.gateway or f"via {provider.gateway}" in proc.stdout
    )


def ensure_tracking_routes(force: bool = False) -> None:
    with lock:
        providers = list(state["providers"])
        installed = dict(state["installed_tracking_routes"])

    normalized, warnings = validate_and_normalize_targets(providers)
    desired = {
        target: provider
        for provider in providers
        if provider.gateway
        for target in provider.targets
    }

    changed = normalized
    for target, old_iface in installed.items():
        provider = desired.get(target)
        if not provider or provider.iface != old_iface:
            target_net = str(ipaddress.ip_network(f"{target}/32", strict=False))
            run_quiet(["ip", "-4", "route", "del", target_net])
            changed = True

    for target, provider in desired.items():
        if force or not tracking_route_matches(target, provider):
            run_best_effort(route_args_for_target(target, provider))
            changed = True

    with lock:
        state["installed_tracking_routes"] = {
            target: provider.iface for target, provider in desired.items()
        }
        state["warnings"] = warnings

    if normalized:
        save_profiles()
    if changed:
        run_quiet(["ip", "-4", "route", "flush", "cache"])


def route_points_to_provider(route: Dict[str, Any], provider: Provider) -> bool:
    return route.get("iface") == provider.iface and route.get("gateway") == provider.gateway


def desired_default_metrics(providers: List[Provider], active_key: str) -> Dict[str, int]:
    metrics = {active_key: 10}
    inactive = sorted(
        (provider for provider in providers if provider.key != active_key and provider.gateway),
        key=lambda item: (item.priority, item.label.lower(), item.key),
    )
    metrics.update({provider.key: 500 + index for index, provider in enumerate(inactive)})
    return metrics


def default_routes_need_update(active: Provider, providers: List[Provider]) -> bool:
    routes = default_routes_snapshot()
    managed = [provider for provider in providers if provider.gateway]
    managed_ifaces = {provider.iface for provider in managed}
    desired_metrics = desired_default_metrics(managed, active.key)

    for provider in managed:
        matches = [route for route in routes if route_points_to_provider(route, provider)]
        expected_metric = desired_metrics[provider.key]
        if len(matches) != 1 or matches[0]["metric"] != expected_metric:
            return True

    return any(
        route.get("iface") in managed_ifaces
        and not any(route_points_to_provider(route, provider) for provider in managed)
        for route in routes
    )


def enforce_active_default_route(active_key: Optional[str], force: bool = False) -> None:
    with lock:
        providers = list(state["providers"])
    active = next((item for item in providers if item.key == active_key), None)
    if not active or not active.gateway:
        return
    if not force and not default_routes_need_update(active, providers):
        return

    for provider in providers:
        delete_default_routes_for_provider(provider)

    desired_metrics = desired_default_metrics(providers, active.key)
    for provider in providers:
        if provider.gateway:
            run_best_effort(default_route_args(provider, desired_metrics[provider.key]))
    run_quiet(["ip", "-4", "route", "flush", "cache"])


def restore_default_routes() -> None:
    with lock:
        providers = list(state["providers"])
    for provider in providers:
        if provider.gateway:
            metric = provider.original_metric or (100 + provider.priority * 100)
            run_best_effort(default_route_args(provider, metric))
    run_quiet(["ip", "-4", "route", "flush", "cache"])


def cleanup() -> None:
    global cleanup_completed
    with cleanup_lock:
        if cleanup_completed:
            return
        cleanup_completed = True
    clear_legacy_firewall_rules()
    with lock:
        targets = list(state["installed_tracking_routes"])
    for target in targets:
        target_net = str(ipaddress.ip_network(f"{target}/32", strict=False))
        run_quiet(["ip", "-4", "route", "del", target_net])
    restore_default_routes()


def parse_ping_output(output: str, provider: Provider) -> Dict[str, Any]:
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", output)
    loss = int(float(loss_match.group(1))) if loss_match else 100
    rtt_match = re.search(
        r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
        output,
    )
    ping = float(rtt_match.group(2)) if rtt_match else 0.0
    jitter = float(rtt_match.group(4)) if rtt_match else 0.0
    ok = loss < 100
    quality_ok = ok and loss <= provider.loss_threshold and (
        ping == 0.0 or ping <= provider.ping_threshold
    )
    return {"ok": ok, "quality_ok": quality_ok, "ping": ping, "jitter": jitter, "loss": loss}


def ping_target(provider: Provider, target: str) -> Dict[str, Any]:
    cfg = state["config"]
    args = [
        "ping",
        "-I",
        provider.iface,
        "-c",
        str(int(cfg["ping_count"])),
        "-W",
        str(int(cfg["ping_timeout_seconds"])),
        "-q",
        target,
    ]
    timeout = int(cfg["ping_count"]) * int(cfg["ping_timeout_seconds"]) + 3
    proc = run_quiet(args, timeout=timeout)
    if not proc:
        return {
            "target": target,
            "ok": False,
            "quality_ok": False,
            "ping": 0.0,
            "jitter": 0.0,
            "loss": 100,
        }
    result = parse_ping_output(f"{proc.stdout}\n{proc.stderr}", provider)
    result["target"] = target
    return result


def measure_provider(provider: Provider) -> Dict[str, Any]:
    if not provider.link_up or not provider.gateway or not provider.targets:
        return {
            "ping": 0.0,
            "jitter": 0.0,
            "loss": 100,
            "status": STATUS_DOWN,
            "checked_targets": [],
        }

    samples = [ping_target(provider, target) for target in provider.targets]
    reachable = [item for item in samples if item["ok"]]
    quality_ok = [item for item in samples if item["quality_ok"]]
    if len(quality_ok) >= provider.required_targets:
        status = STATUS_STABLE
    elif len(reachable) >= provider.required_targets:
        status = STATUS_DEGRADED
    else:
        status = STATUS_DOWN

    aggregate = quality_ok or reachable or samples
    ping_values = [item["ping"] for item in aggregate if item["ping"] > 0]
    jitter_values = [item["jitter"] for item in aggregate if item["jitter"] > 0]
    loss_values = [item["loss"] for item in aggregate]
    return {
        "ping": round(sum(ping_values) / len(ping_values), 1) if ping_values else 0.0,
        "jitter": round(sum(jitter_values) / len(jitter_values), 1) if jitter_values else 0.0,
        "loss": round(sum(loss_values) / len(loss_values)) if loss_values else 100,
        "status": status,
        "checked_targets": samples,
    }


def update_provider_counters(provider: Provider, usable_for_switching: bool) -> None:
    if usable_for_switching:
        provider.recover_count += 1
        provider.fail_count = 0
    else:
        provider.fail_count += 1
        provider.recover_count = 0


def cooldown_passed(now: float) -> bool:
    return now - float(state["last_switch"]) >= int(state["config"]["switch_cooldown_seconds"])


def sorted_providers() -> List[Provider]:
    with lock:
        return sorted(state["providers"], key=lambda item: (item.priority, item.label.lower(), item.key))


def best_available_provider(require_recovery: bool) -> Optional[Provider]:
    providers = sorted_providers()
    cfg = state["config"]
    required = int(cfg["up_successes"]) if require_recovery else 0
    for provider in providers:
        if provider.stats["status"] == STATUS_STABLE and provider.recover_count >= required:
            return provider
    if cfg.get("failover_on_degraded", True):
        for provider in providers:
            if provider.stats["status"] == STATUS_DEGRADED and provider.recover_count >= required:
                return provider
    return None


def set_global_internet_source(source: str, reason: str = "") -> bool:
    provider = provider_by_key(source)
    if not provider or not provider.gateway:
        with lock:
            state["last_error"] = f"Провайдер {source} недоступний або не має gateway."
        return False

    with lock:
        already_active = state["active_provider"] == source
    if already_active:
        enforce_active_default_route(source)
        return True

    ensure_tracking_routes()
    enforce_active_default_route(source, force=True)
    if state["config"].get("flush_conntrack", True):
        run_best_effort(["conntrack", "-F"])

    with lock:
        state["active_provider"] = source
        state["active_interface"] = provider.iface
        state["last_switch"] = time.time()
    logger.info("Перемикання на %s (%s): %s", provider.label, provider.iface, reason or "manual")
    return True


def choose_auto_provider(now: float) -> Optional[str]:
    providers = sorted_providers()
    if not providers:
        return None
    with lock:
        active_key = state["active_provider"]
        cfg = dict(state["config"])

    active = next((item for item in providers if item.key == active_key), None)
    highest_priority = providers[0]
    if not active:
        candidate = best_available_provider(require_recovery=False)
        return candidate.key if candidate else None

    if active.stats["status"] == STATUS_DOWN:
        candidate = best_available_provider(require_recovery=False)
        if candidate and candidate.key != active.key:
            return candidate.key

    if active.key != highest_priority.key and highest_priority.stats["status"] == STATUS_STABLE:
        if (
            highest_priority.recover_count >= int(cfg["primary_recovery_successes"])
            and cooldown_passed(now)
        ):
            return highest_priority.key

    if active.fail_count >= int(cfg["down_failures"]) and cooldown_passed(now):
        candidate = best_available_provider(require_recovery=True)
        if candidate and candidate.key != active.key:
            return candidate.key
    return None


def monitor_once(blocking: bool = False) -> None:
    if not monitor_lock.acquire(blocking=blocking):
        return
    started = time.monotonic()
    try:
        _monitor_once()
    finally:
        with lock:
            state["last_check_at"] = time.time()
            state["last_check_duration_ms"] = round((time.monotonic() - started) * 1000)
        monitor_lock.release()


def _monitor_once() -> None:
    success, topology_changed = discover_providers()
    if not success:
        return

    now = time.time()
    with lock:
        route_audit_due = now - state["last_route_audit"] >= int(state["config"]["route_audit_seconds"])
        providers = list(state["providers"])
        cfg = dict(state["config"])
    if topology_changed or route_audit_due:
        ensure_tracking_routes(force=topology_changed)
        with lock:
            state["last_route_audit"] = now

    previous_statuses = {provider.key: provider.stats.get("status") for provider in providers}
    if len(providers) == 1:
        measurements = [measure_provider(providers[0])]
    elif providers:
        workers = min(len(providers), 8)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            measurements = list(executor.map(measure_provider, providers))
    else:
        measurements = []

    if measurements:
        with lock:
            current_by_key = {item.key: item for item in state["providers"]}
            for provider, measured in zip(providers, measurements):
                current = current_by_key.get(provider.key)
                if not current:
                    continue
                current.stats = measured
                usable = measured["status"] == STATUS_STABLE or (
                    measured["status"] == STATUS_DEGRADED and not cfg["failover_on_degraded"]
                )
                update_provider_counters(current, usable)
                previous_status = previous_statuses.get(current.key)
                if previous_status != measured["status"]:
                    logger.info(
                        "Стан %s (%s): %s, loss=%s%%, ping=%sms",
                        current.label,
                        current.iface,
                        measured["status"],
                        measured["loss"],
                        measured["ping"],
                    )

    with lock:
        mode = state["mode"]
        active_key = state["active_provider"]

    selected: Optional[str] = None
    mode_changed = False
    if mode == "auto":
        selected = choose_auto_provider(now)
    elif mode.startswith("manual:"):
        manual_key = mode.split(":", 1)[1]
        manual_provider = provider_by_key(manual_key)
        if not manual_provider or (
            cfg["auto_fallback"] and manual_provider.stats["status"] == STATUS_DOWN
        ):
            with lock:
                state["mode"] = "auto"
            mode_changed = True
            selected = choose_auto_provider(now)
        else:
            selected = manual_key

    if selected:
        set_global_internet_source(selected, reason=f"mode={state['mode']}")
    else:
        enforce_active_default_route(active_key)
    if mode_changed:
        save_profiles()


def monitor_loop() -> None:
    while not stop_event.is_set():
        try:
            monitor_once()
        except Exception as exc:
            with lock:
                state["last_error"] = str(exc)
            logger.exception("Помилка циклу моніторингу")
        interval = int(state["config"]["interval_seconds"])
        stop_event.wait(max(interval, 1))


def provider_to_dict(provider: Provider) -> Dict[str, Any]:
    return {
        "identity": provider.identity,
        "key": provider.key,
        "iface": provider.iface,
        "gateway": provider.gateway,
        "local_ip": provider.local_ip,
        "label": provider.label,
        "hardware_name": provider.hardware_name,
        "hardware_type": provider.hardware_type,
        "hardware_model": provider.hardware_model,
        "driver": provider.driver,
        "bus": provider.bus,
        "priority": provider.priority,
        "present": True,
        "link_up": provider.link_up,
        "targets": provider.targets,
        "ping_threshold": provider.ping_threshold,
        "loss_threshold": provider.loss_threshold,
        "required_targets": provider.required_targets,
        "fail_count": provider.fail_count,
        "recover_count": provider.recover_count,
        "stats": provider.stats,
    }


def public_state() -> Dict[str, Any]:
    with lock:
        return {
            "mode": state["mode"],
            "active_provider": state["active_provider"],
            "active_interface": state["active_interface"],
            "last_switch": state["last_switch"],
            "started_at": state["started_at"],
            "last_check_at": state["last_check_at"],
            "last_check_duration_ms": state["last_check_duration_ms"],
            "last_error": state["last_error"],
            "warnings": list(state["warnings"]),
            "config": dict(state["config"]),
            "providers": [provider_to_dict(item) for item in state["providers"]],
        }


def parse_targets(value: Any, fallback: List[str]) -> List[str]:
    raw = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    valid: List[str] = []
    for item in raw:
        target = str(item).strip()
        if not target:
            continue
        try:
            ipaddress.IPv4Address(target)
        except ValueError:
            continue
        valid.append(target)
    return valid or fallback


def update_profile_from_provider(provider: Provider) -> None:
    profile = state["profiles"].get(provider.identity)
    if profile is None:
        return
    profile.update(
        {
            "key": provider.key,
            "iface": provider.iface,
            "gateway": provider.gateway,
            "label": provider.label,
            "priority": provider.priority,
            "original_metric": provider.original_metric,
            "targets": provider.targets,
            "ping_threshold": provider.ping_threshold,
            "loss_threshold": provider.loss_threshold,
            "required_targets": provider.required_targets,
        }
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.after_request
def add_response_headers(response):  # type: ignore[no-untyped-def]
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/api/status")
def get_status():
    return jsonify(public_state())


@app.route("/api/health")
def health():
    with lock:
        healthy = not state["last_error"] and bool(state["providers"])
        checked_at = state["last_check_at"]
    return jsonify({"status": "ok" if healthy else "degraded", "checked_at": checked_at}), (
        200 if healthy else 503
    )


@app.route("/api/mode", methods=["POST"])
def change_mode():
    req = request.get_json(silent=True) or {}
    mode = req.get("mode")
    if mode != "auto" and not (isinstance(mode, str) and mode.startswith("manual:")):
        return jsonify({"status": "error", "message": "Невідомий режим"}), 400
    if isinstance(mode, str) and mode.startswith("manual:"):
        if not provider_by_key(mode.split(":", 1)[1]):
            return jsonify({"status": "error", "message": "Провайдер не знайдений"}), 400
    with lock:
        state["mode"] = mode
    monitor_once(blocking=True)
    save_profiles()
    return jsonify({"status": "success", "state": public_state()})


@app.route("/api/config", methods=["POST"])
def update_config():
    req = request.get_json(silent=True) or {}
    numeric_config = {
        "interval_seconds": (2, 300, int),
        "ping_count": (1, 10, int),
        "ping_timeout_seconds": (1, 20, int),
        "down_failures": (1, 20, int),
        "up_successes": (1, 20, int),
        "primary_recovery_successes": (1, 30, int),
        "switch_cooldown_seconds": (0, 3600, int),
        "route_audit_seconds": (10, 3600, int),
    }

    with lock:
        for key in ("auto_fallback", "failover_on_degraded", "flush_conntrack"):
            if key in req:
                state["config"][key] = bool(req[key])
        for key, (minimum, maximum, caster) in numeric_config.items():
            if key in req:
                try:
                    state["config"][key] = max(minimum, min(maximum, caster(req[key])))
                except (TypeError, ValueError):
                    pass

        payload = req.get("providers", {})
        if isinstance(payload, dict):
            for provider in state["providers"]:
                item = payload.get(provider.key)
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("label"), str):
                    provider.label = item["label"].strip() or provider.hardware_name
                provider.targets = parse_targets(item.get("targets"), provider.targets)
                for attr, minimum, maximum, caster in (
                    ("priority", 1, 999, int),
                    ("ping_threshold", 1.0, 5000.0, float),
                    ("loss_threshold", 0, 100, int),
                    ("required_targets", 1, 10, int),
                ):
                    if attr not in item:
                        continue
                    try:
                        value = max(minimum, min(maximum, caster(item[attr])))
                        setattr(provider, attr, value - 1 if attr == "priority" else value)
                    except (TypeError, ValueError):
                        pass
                update_profile_from_provider(provider)
            state["providers"].sort(key=lambda item: (item.priority, item.label.lower(), item.key))

    save_profiles()
    ensure_tracking_routes()
    enforce_active_default_route(state["active_provider"])
    return jsonify({"status": "success", "state": public_state()})


@app.route("/api/rescan", methods=["POST"])
def rescan_interfaces():
    monitor_once(blocking=True)
    with lock:
        failed = bool(state["last_error"]) and not state["providers"]
    if failed:
        return jsonify({"status": "error", "state": public_state()}), 500
    return jsonify({"status": "success", "state": public_state()})


def shutdown_handler(signum, frame) -> None:  # type: ignore[no-untyped-def]
    stop_event.set()
    raise SystemExit(0)


def main() -> None:
    require_root()
    clear_legacy_firewall_rules()
    load_profiles()
    success, changed = discover_providers()
    if not success:
        logger.warning("%s", state["last_error"])
    ensure_tracking_routes(force=changed)
    candidate = best_available_provider(require_recovery=False)
    if not candidate:
        providers = sorted_providers()
        candidate = providers[0] if providers else None
    if candidate:
        set_global_internet_source(candidate.key, reason="startup")

    worker = threading.Thread(target=monitor_loop, daemon=True)
    worker.start()
    try:
        from waitress import serve
    except ImportError:
        logger.error("Waitress не встановлено. Виконайте: pip install -r requirements.txt")
        raise SystemExit(1)

    host = os.environ.get("WAN_BIND", "0.0.0.0")
    port = int(os.environ.get("WAN_PORT", "5000"))
    threads = max(2, min(int(os.environ.get("WAN_THREADS", "4")), 16))
    logger.info("Панель запущена на http://%s:%s, WSGI threads=%s", host, port, threads)
    try:
        serve(
            app,
            host=host,
            port=port,
            threads=threads,
            ident="MultiWAN",
            channel_timeout=30,
            cleanup_interval=10,
            connection_limit=50,
            max_request_body_size=128 * 1024,
            expose_tracebacks=False,
        )
    finally:
        stop_event.set()
        worker.join(timeout=5)
        cleanup()


atexit.register(cleanup)
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


if __name__ == "__main__":
    main()
