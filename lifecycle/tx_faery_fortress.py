#!/usr/bin/env python3
# ffp: TX-FAERY-FORTRESS glue python
"""
tx_faery_fortress.py -- Ndimmôrwyn (revenue engine) lifecycle.
Alias: tx_ndimmorwyn.py. Defensive structure is Faery Fortress.

Commands:
  status                 -- fortress-wide status: RGE/RGO/RGU tree + mission gates
  launch <rgu_id>        -- launch an RGU (opens allocation via bridge, starts SLA clock)
  enforce                -- Lady Naga gate: check 72h/7d/14d deadlines, act on failures
  autopsy <rgu_id>       -- cause-of-death analysis for a shutdown RGU
  repurpose <rgu_id> --target <rgu_id>   -- re-home FFPs into a surviving RGU
  retool <rgu_id>        -- keep RGU, rework failing FFP after autopsy
  synergy                -- compute synergy scores for RGOs/RGEs (Composition Law)
  bechem-report          -- emit the weekly air-gapped critique package
  mission                -- 72h / 7d / 14d mission tracker vs the revenue ledger

Reads docs/faery_fortress/registry.json; queries db/portfolio.sqlite3
rgu_revenue_events for realized revenue. Never invents revenue: if the
ledger has no row, it did not happen (Veritas-Rex).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tx_observability as obs  # noqa: E402  (path bootstrap must run first)

log = obs.get_logger("tx_faery_fortress")

# BASE must be MACHINE-RELATIVE: this script runs on the hub as well as on the
# phone, and a hardcoded phone path made the hub read a tree that does not
# exist there (the lifecycle gate failed every hour with a bare KeyError).
# The script lives in <BASE>/bin/, so the tree root is its parent's parent.
BASE = Path(os.environ.get("TX_BASE_DIR", str(Path(__file__).resolve().parents[1])))
REGISTRY = BASE / "docs" / "faery_fortress" / "registry.json"
DB = BASE / "db" / "portfolio.sqlite3"
BRIDGE = os.environ.get("TX_BRIDGE", "http://127.0.0.1:9199")

LAUNCH_TS_FILE = BASE / "logs" / "fortress_launch_ts.json"
MISSION_TS_FILE = BASE / "logs" / "fortress_mission_ts.json"


# The RGU roster and the ASSET CATALOGUE share the path
# docs/faery_fortress/registry.json in the two trees: the workspace's copy
# carries the "rgus" roster, while the HUB's copy is the asset catalogue (a
# different schema, written by the catalog publisher). Reading the wrong one
# raised a bare KeyError: 'rgus' and the lifecycle gate failed every hour.
# So the loader resolves whichever file actually carries the roster, and
# _save writes back to the SAME file it loaded.
RGU_REGISTRY_CANDIDATES = (
    REGISTRY,
    BASE / "docs" / "faery_fortress" / "rgu_registry.json",
)
_REGISTRY_PATH: Optional[Path] = None


def _load() -> Dict[str, Any]:
    """Load the registry that actually carries the RGU roster."""
    global _REGISTRY_PATH
    for path in RGU_REGISTRY_CANDIDATES:
        try:
            reg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("registry %s unreadable: %s", path, exc)
            continue
        if isinstance(reg, dict) and "rgus" in reg:
            _REGISTRY_PATH = path
            return reg
        log.warning("registry %s has no 'rgus' roster (different schema)", path)
    raise SystemExit(
        "tx_faery_fortress: no RGU roster found in %s -- deploy the workspace "
        "registry as rgu_registry.json" % ", ".join(str(p) for p in RGU_REGISTRY_CANDIDATES)
    )


def _save(reg: Dict[str, Any]) -> None:
    path = _REGISTRY_PATH or REGISTRY
    path.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now() -> float:
    return time.time()


def _revenue_split(since_ts: Optional[float] = None) -> Dict[str, float]:
    """Split realized revenue into REAL money (stripe/sponsorship/fee events
    backed by an actual payment rail) vs PAPER (yield/pnl from internal
    simulation).  Veritas-Rex: a webhook-backed row is real; a paper pnl
    row is not, no matter how big the number is."""
    if not DB.exists():
        return {"real_usd": 0.0, "paper_usd": 0.0}
    con = sqlite3.connect(DB)
    try:
        q = "select kind, coalesce(sum(amount_usd),0) from rgu_revenue_events"
        args: tuple = ()
        if since_ts is not None:
            q += " where ts >= ?"
            args = (since_ts,)
        q += " group by kind"
        real_kinds = {"subscription", "sponsorship", "fee", "rebate"}
        real = paper = 0.0
        for kind, amt in con.execute(q, args):
            if kind in real_kinds:
                real += float(amt or 0)
            else:
                paper += float(amt or 0)
        return {"real_usd": round(real, 2), "paper_usd": round(paper, 2)}
    finally:
        con.close()


def _revenue_since(ts: float) -> float:
    """Sum realized revenue events since *ts*. Veritas: ledger only."""
    if not DB.exists():
        return 0.0
    con = sqlite3.connect(DB)
    try:
        row = con.execute(
            "select coalesce(sum(amount_usd),0) from rgu_revenue_events where ts >= ?",
            (ts,),
        ).fetchone()
        return float(row[0] or 0)
    finally:
        con.close()


def _revenue_by_template() -> Dict[str, float]:
    if not DB.exists():
        return {}
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "select template_id, coalesce(sum(amount_usd),0) from rgu_revenue_events group by template_id"
        ).fetchall()
        return {r[0]: float(r[1] or 0) for r in rows}
    finally:
        con.close()


def _load_ts(path: Path) -> Optional[float]:
    try:
        return json.loads(path.read_text())["ts"]
    except (OSError, ValueError, KeyError):
        return None


def _save_ts(path: Path, ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": ts, "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat()}))


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def launch(rgu_id: str) -> dict:
    reg = _load()
    rgu = next((r for r in reg["rgus"] if r["id"] == rgu_id), None)
    if not rgu:
        return {"ok": False, "error": f"unknown rgu: {rgu_id}"}
    if rgu["capex_usd"] > reg["mission"]["max_capex_usd"]:
        return {"ok": False, "error": f"capex ${rgu['capex_usd']} exceeds mission max ${reg['mission']['max_capex_usd']}"}

    ts = _now()
    launches = {}
    if LAUNCH_TS_FILE.exists():
        try:
            launches = json.loads(LAUNCH_TS_FILE.read_text())
        except ValueError:
            launches = {}
    launches[rgu_id] = ts
    _save_ts(LAUNCH_TS_FILE, ts)  # last-launch marker
    LAUNCH_TS_FILE.write_text(json.dumps(launches, indent=1))

    rgu["status"] = "LAUNCHED"
    rgu["launched_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    _save(reg)
    log.info("launched %s (capex=$%.2f, sla=%dh)", rgu_id, rgu["capex_usd"], rgu["sla_first_revenue_h"])
    return {"ok": True, "rgu": rgu_id, "sla_first_revenue_h": rgu["sla_first_revenue_h"], "profit_gate_d": rgu["profit_gate_d"]}


# ---------------------------------------------------------------------------
# Lady Naga enforcement gates
# ---------------------------------------------------------------------------

def enforce(dry_run: bool = False) -> dict:
    reg = _load()
    launches = {}
    if LAUNCH_TS_FILE.exists():
        try:
            launches = json.loads(LAUNCH_TS_FILE.read_text())
        except ValueError:
            launches = {}
    rev_by_tpl = _revenue_by_template()
    now = _now()
    actions: List[Dict[str, Any]] = []

    for rgu in reg["rgus"]:
        rid = rgu["id"]
        started = launches.get(rid)
        if not started:
            continue
        rev = rev_by_tpl.get(rid, 0.0) + rev_by_tpl.get(rgu.get("template_hint", ""), 0.0)
        hours = (now - started) / 3600.0
        days = hours / 24.0

        # Gate 1: 72h first-revenue SLA
        if rgu["status"] in ("LAUNCHED", "RETOOLED") and hours >= 72 and rev <= 0:
            actions.append({"gate": "first_revenue_72h", "rgu": rid, "verdict": "FAIL",
                            "detail": f"{hours:.1f}h, $0 revenue -> SHUTDOWN (repurpose)"})
            if not dry_run:
                rgu["status"] = "SHUTDOWN"
                rgu["shutdown_reason"] = "no_revenue_72h"
            continue

        # Gate 2: 7d profit gate
        if rgu["status"] in ("LAUNCHED", "EARNING", "RETOOLED") and days >= rgu["profit_gate_d"]:
            if rev <= rgu["capex_usd"]:
                actions.append({"gate": "profit_gate_7d", "rgu": rid, "verdict": "FAIL",
                                "detail": f"${rev:.2f} <= capex ${rgu['capex_usd']:.2f} after {days:.1f}d -> SHUTDOWN"})
                if not dry_run:
                    rgu["status"] = "SHUTDOWN"
                    rgu["shutdown_reason"] = "unprofitable_7d"
            else:
                if rgu["status"] != "PROFITABLE":
                    actions.append({"gate": "profit_gate_7d", "rgu": rid, "verdict": "PASS",
                                    "detail": f"${rev:.2f} > capex -> PROFITABLE"})
                    if not dry_run:
                        rgu["status"] = "PROFITABLE"
        elif rgu["status"] == "LAUNCHED" and rev > 0:
            if not dry_run:
                rgu["status"] = "EARNING"

    if not dry_run:
        _save(reg)
    return {"ok": True, "actions": actions, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# Autopsy / repurpose / retool
# ---------------------------------------------------------------------------

def autopsy(rgu_id: str) -> dict:
    reg = _load()
    rgu = next((r for r in reg["rgus"] if r["id"] == rgu_id), None)
    if not rgu:
        return {"ok": False, "error": f"unknown rgu: {rgu_id}"}
    launches = {}
    if LAUNCH_TS_FILE.exists():
        try:
            launches = json.loads(LAUNCH_TS_FILE.read_text())
        except ValueError:
            launches = {}
    started = launches.get(rgu_id)
    rev = _revenue_by_template().get(rgu_id, 0.0)
    lived_days = ((_now() - started) / 86400.0) if started else 0.0
    report = {
        "rgu": rgu_id,
        "status": rgu["status"],
        "shutdown_reason": rgu.get("shutdown_reason", "n/a"),
        "capex_usd": rgu["capex_usd"],
        "realized_usd": rev,
        "lived_days": round(lived_days, 2),
        "ffps": rgu["ffps"],
        "icp": rgu["icp"],
        "market_feedback": "no purchase within SLA window; demand-side signal negative at current price/channel",
        "cause_class": "DEMAND" if rev == 0 else "UNIT_ECONOMICS",
        "disposition_options": ["repurpose", "retool"],
    }
    out = BASE / "logs" / "autopsy" / f"{rgu_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return {"ok": True, "report": str(out), **report}


def repurpose(rgu_id: str, target: str) -> dict:
    reg = _load()
    src = next((r for r in reg["rgus"] if r["id"] == rgu_id), None)
    dst = next((r for r in reg["rgus"] if r["id"] == target), None)
    if not src or not dst:
        return {"ok": False, "error": "unknown rgu"}
    moved = [f for f in src["ffps"] if f not in dst["ffps"]]
    dst["ffps"].extend(moved)
    src["status"] = "REPURPOSED"
    src["ffps_rehomed_to"] = target
    _save(reg)
    log.info("repurposed %s -> %s (%d FFPs moved)", rgu_id, target, len(moved))
    return {"ok": True, "moved_ffps": moved, "target": target}


def retool(rgu_id: str) -> dict:
    reg = _load()
    rgu = next((r for r in reg["rgus"] if r["id"] == rgu_id), None)
    if not rgu:
        return {"ok": False, "error": "unknown rgu"}
    rgu["status"] = "RETOOLED"
    rgu["retooled_at"] = datetime.now(timezone.utc).isoformat()
    rgu.pop("shutdown_reason", None)
    _save(reg)
    return {"ok": True, "rgu": rgu_id, "status": "RETOOLED"}


# ---------------------------------------------------------------------------
# Synergy scoring (Composition Law)
# ---------------------------------------------------------------------------

def synergy() -> dict:
    reg = _load()
    rgus = {r["id"]: r for r in reg["rgus"]}
    out = {}
    for rgo_id, rgo in reg["rgos"].items():
        members = [rgus[m] for m in rgo["members"] if m in rgus]
        if not members:
            continue
        ffps = [f for m in members for f in m["ffps"]]
        shared = len(ffps) - len(set(ffps))
        total = max(1, len(ffps))
        sharing = shared / total                       # FFP reuse density
        icps = {m["icp"] for m in members}
        icp_purity = 1.0 if len(icps) == 1 else 0.5    # one ICP per organism
        capex = sum(m["capex_usd"] for m in members)
        score = round(1.0 + sharing * 0.5 + (icp_purity - 1) * 0.3, 3)
        out[rgo_id] = {
            "members": len(members),
            "ffp_reuse": f"{shared}/{total}",
            "icps": sorted(icps),
            "capex_usd": capex,
            "synergy_score": score,
            "composition_law": "PASS" if score >= 1.15 else "FAIL(dissolve+recompose)",
        }
    rge_out = {}
    for rge_id, rge in reg["rges"].items():
        member_scores = [out[m]["synergy_score"] for m in rge["members"] if m in out]
        rge_out[rge_id] = {
            "rgos": len(rge["members"]),
            "mean_synergy": round(sum(member_scores) / max(1, len(member_scores)), 3),
        }
    return {"ok": True, "rgos": out, "rges": rge_out}


# ---------------------------------------------------------------------------
# Mission tracker + Bechem
# ---------------------------------------------------------------------------

def mission() -> dict:
    reg = _load()
    m = reg["mission"]
    t0 = _load_ts(MISSION_TS_FILE)
    if t0 is None:
        t0 = _now()
        _save_ts(MISSION_TS_FILE, t0)
    now = _now()
    elapsed_h = (now - t0) / 3600.0
    realized = _revenue_since(t0)
    split = _revenue_split(t0)
    total_rev = _revenue_by_template()
    all_time = sum(total_rev.values())
    launches = {}
    if LAUNCH_TS_FILE.exists():
        try:
            launches = json.loads(LAUNCH_TS_FILE.read_text())
        except ValueError:
            launches = {}

    gates = {
        "first_revenue_72h": {
            "deadline_h": 72, "elapsed_h": round(elapsed_h, 1),
            "realized_usd": round(realized, 2),
            "real_usd": split["real_usd"],
            "paper_usd": split["paper_usd"],
            "verdict": "PENDING" if elapsed_h < 72 else ("PASS" if split["real_usd"] > 0 else "FAIL"),
        },
        "profit_gate_7d": {
            "note": "per-RGU, see enforce",
        },
        "target_14d": {
            "target_usd": m["target_usd"],
            "deadline_d": m["target_deadline_d"],
            "elapsed_d": round(elapsed_h / 24.0, 2),
            "realized_usd": round(realized, 2),
            "real_usd": split["real_usd"],
            "paper_usd": split["paper_usd"],
            "pct": round(100.0 * split["real_usd"] / m["target_usd"], 1),
            "required_run_rate_usd_day": round(m["target_usd"] / max(1.0, m["target_deadline_d"] - elapsed_h / 24.0), 2),
            "verdict": "ON TRACK" if split["real_usd"] >= m["target_usd"] * (elapsed_h / 24.0) / m["target_deadline_d"] else "BEHIND",
        },
    }
    return {
        "mission_start": datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        "launched_rgus": sorted(launches.keys()),
        "revenue_by_rgu": {k: round(v, 2) for k, v in sorted(total_rev.items(), key=lambda x: -x[1])},
        "all_time_ledger_usd": round(all_time, 2),
        "all_time_ledger_usd_note": "[PAPER] all events are template/paper revenue — 0 real transactions",
        "gates": gates,
    }


def bechem_report() -> dict:
    _load()  # validates the registry before the report is assembled
    syn = synergy()
    mis = mission()
    enf = enforce(dry_run=True)
    pkg = {
        "package": "Bechem Weekly Critique Request",
        "generated": datetime.now(timezone.utc).isoformat(),
        "fortress": "Ndimmôrwyn (Tianxia)",
        "ndimmorwyn": "Ndimmôrwyn (Tianxia)",
        "mission": mis,
        "synergy": syn,
        "pending_gate_actions": enf["actions"],
        "questions_for_bechem": [
            "Which RGO is weakest on the Composition Law and why?",
            "Is the $3,000/14d trajectory structurally sound or demand-limited?",
            "Which FFP is over-weighted and should be re-homed first?",
        ],
        "emperor_note": "carry to Bechem via air-gap; verdicts return to Emperor only",
    }
    out = BASE / "logs" / "bechem" / f"critique_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pkg, indent=2))
    return {"ok": True, "report": str(out)}


# ---------------------------------------------------------------------------
# Status tree
# ---------------------------------------------------------------------------

def status() -> dict:
    reg = _load()
    rgus = {r["id"]: r for r in reg["rgus"]}
    rev = _revenue_by_template()
    tree = {}
    for rge_id, rge in reg["rges"].items():
        tree[rge_id] = {}
        for rgo_id in rge["members"]:
            rgo = reg["rgos"].get(rgo_id, {})
            tree[rge_id][rgo_id] = [
                {"id": rid, "status": rgus[rid]["status"], "capex": rgus[rid]["capex_usd"],
                 "price": rgus[rid]["price_usd"], "icp": rgus[rid]["icp"],
                 "realized_usd": round(rev.get(rid, 0.0), 2)}
                for rid in rgo.get("members", []) if rid in rgus
            ]
    counts: Dict[str, int] = {}
    for r in reg["rgus"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"fortress": "Ndimmôrwyn", "ndimmorwyn": "Ndimmôrwyn", "rgu_status_counts": counts, "tree": tree}


def _cli() -> int:
    p = argparse.ArgumentParser(prog="tx_faery_fortress")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("enforce").add_argument("--dry-run", action="store_true")
    sub.add_parser("synergy")
    sub.add_parser("mission")
    sub.add_parser("bechem-report")
    s_l = sub.add_parser("launch")
    s_l.add_argument("rgu_id")
    s_a = sub.add_parser("autopsy")
    s_a.add_argument("rgu_id")
    s_r = sub.add_parser("repurpose")
    s_r.add_argument("rgu_id")
    s_r.add_argument("--target", required=True)
    s_t = sub.add_parser("retool")
    s_t.add_argument("rgu_id")
    args = p.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
    elif args.cmd == "enforce":
        print(json.dumps(enforce(args.dry_run), indent=2))
    elif args.cmd == "synergy":
        print(json.dumps(synergy(), indent=2))
    elif args.cmd == "mission":
        print(json.dumps(mission(), indent=2))
    elif args.cmd == "bechem-report":
        print(json.dumps(bechem_report(), indent=2))
    elif args.cmd == "launch":
        print(json.dumps(launch(args.rgu_id), indent=2))
    elif args.cmd == "autopsy":
        print(json.dumps(autopsy(args.rgu_id), indent=2))
    elif args.cmd == "repurpose":
        print(json.dumps(repurpose(args.rgu_id, args.target), indent=2))
    elif args.cmd == "retool":
        print(json.dumps(retool(args.rgu_id), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
