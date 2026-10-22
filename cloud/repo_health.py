"""Per-repo PR concurrency limits + circuit-breaker signals.

Implements the repository concurrency policy (2026-08-03, operator-mandated;
knowledge/repo_concurrency_policy.md): maintainer attention is a finite
resource, so the limit is a LOW DEFAULT with degradation on trouble, not a
merit-scaled allowance.

  default             -> ``default_max_open`` (2) open PRs per repo
  degraded            -> ``degraded_max_open`` (1) while ANY of: unresolved
                         CHANGES_REQUESTED; an OPEN auto-shipped PR (one
                         autonomous PR at a time until it merges); a breaker
                         trip recorded for the repo; every open PR of ours
                         stale > ``stale_activity_days`` with zero maintainer
                         engagement (declining responsiveness)
  close-unmerged      -> capacity 0 for ``close_cooldown_days`` — KEPT
                         STRICTER than the policy's degraded-1 (never weaken)
  third slot          -> OPERATOR-ONLY, via scripts/preship_check.py with a
                         recorded justification; the automated path never
                         takes it
  absolute ceiling    -> ``absolute_max_open`` (3); beyond it only an
                         explicit, recorded operator override
  per-repo policy     -> ``executor.repo_policy[repo]`` may LOWER a repo's
                         limit (``max_open``) and may withdraw it from the
                         unattended auto-ship path (``autoship: false``).
                         It can never raise a limit or re-enable a repo the
                         rest of this module has already restricted.

Note on CHANGES_REQUESTED at degraded-1 vs the old forced-0: for shipping
they are behaviorally identical — the CR PR itself is open and occupies the
single slot, so remaining() is 0 either way until it resolves.

Fail-closed asymmetry, deliberate:
- SHIPPING callers (executor, autoship gates): any unreadable input ->
  capacity 0 with a reason. A corrupt state file must never enable a ship.
- SCOUTING callers (discovery only, no outward action): unreadable health
  falls back to the legacy fixed limit so a corrupt file cannot silently
  kill discovery.

The breaker: any ledger PR with shipped_by == "autoship" observed CLOSED (not
MERGED) trips auto-shipping off until the operator manually re-arms
cloud/circuit_state.json. This module only DETECTS the trip condition; the
executor persists the trip. Nothing in this module writes armed=true.
"""

import datetime
import json
import os
import re

CLOUD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CLOUD)
OUTCOMES = os.path.join(CLOUD, "repo_outcomes.json")
MONITOR_STATE = os.path.join(CLOUD, "monitor_state.json")
CIRCUIT = os.path.join(CLOUD, "circuit_state.json")
TRACKED = os.path.join(os.path.dirname(CLOUD), "ops_tracked_prs.json")

DEFAULTS = {"default_max_open": 2, "absolute_max_open": 3,
            "degraded_max_open": 1, "stale_activity_days": 14,
            "close_cooldown_days": 14, "repo_policy": {},
            # legacy keys, accepted so older configs don't error; capacity()
            # no longer uses them (repo-concurrency policy 2026-08-03)
            "base_capacity": 1, "capacity_per_merge": 1, "capacity_cap": 3}


def load_outcomes(path=OUTCOMES):
    return json.load(open(path))


def save_outcomes(doc, path=OUTCOMES):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _repo_of(pr_key):
    return pr_key.split(" PR ")[0]


# The ONLY intent resolution that proves nothing left the machine. Everything
# else — unresolved (None), "shipped", a push/PR failure, or any outcome string
# a future version invents — burns its daily outward slot. Single definition:
# the executor's budget arithmetic and the gates' daily_cap must never drift
# apart, which is exactly how ship_error_postpush escaped the cap (audit H3).
NO_OUTWARD_RESOLUTION = "no_outward"


def intent_is_outward(resolved):
    """True if this write-ahead intent must count against the daily cap."""
    return resolved != NO_OUTWARD_RESOLUTION


def _open_prs_by_repo(monitor_state, outcomes=None):
    """Open PRs of ours per repo. `outcomes` (the ledger) is not optional for
    ship-time callers: a PR auto-shipped earlier TODAY is not in monitor_state
    until tomorrow's fetch, so counting only monitor_state lets a second ship
    into a repo that is already at capacity (audit H4)."""
    counts, changes_requested = {}, {}
    live = monitor_state.get("state") or {}
    for key, v in live.items():
        repo = _repo_of(key)
        if v.get("state") == "OPEN":
            counts[repo] = counts.get(repo, 0) + 1
            if v.get("reviewDecision") == "CHANGES_REQUESTED":
                changes_requested.setdefault(repo, []).append(key)
    for entry in (outcomes or {}).get("prs", []):
        key = entry.get("pr")
        # monitor_state is authoritative for anything it knows about; the
        # ledger only adds PRs it has not observed yet
        if not key or key in live or entry.get("state") != "OPEN":
            continue
        repo = _repo_of(key)
        counts[repo] = counts.get(repo, 0) + 1
    # Third source (system audit P2-o, 2026-09-02): OPERATOR ships write
    # only ops_tracked_prs.json until the next morning's monitor fetch —
    # a same-day hand-opened PR was invisible to capacity, so the executor
    # could ship into a repo the operator had just filled. Conservative:
    # a tracked PR unknown to BOTH other sources counts as OPEN until the
    # monitor learns its real state tomorrow. Fail-soft on a bad file.
    try:
        tracked = json.load(open(TRACKED))
        seen_keys = set(live) | {e.get("pr")
                                 for e in (outcomes or {}).get("prs", [])}
        for t in tracked if isinstance(tracked, list) else []:
            key = f"{t.get('repo')} PR {t.get('number')}"
            if t.get("repo") and key not in seen_keys:
                counts[t["repo"]] = counts.get(t["repo"], 0) + 1
                seen_keys.add(key)
    except (OSError, ValueError, TypeError):
        pass
    return counts, changes_requested


def reconcile(outcomes, monitor_state, today):
    """Record OPEN -> MERGED/CLOSED transitions of our PRs; update merge
    counters. Returns (outcomes, [transition strings])."""
    transitions = []
    live = monitor_state.get("state") or {}
    for entry in outcomes.get("prs", []):
        if entry.get("state") != "OPEN":
            continue
        seen = live.get(entry["pr"])
        if not seen:
            continue
        # sticky metrics fields (repo-concurrency policy 7): recorded once,
        # never cleared, so rates and latencies derive from the ledger alone
        if seen.get("reviewDecision") == "CHANGES_REQUESTED":
            entry["changes_requested"] = True
        if "first_activity_day" not in entry and (
                (seen.get("n_reviews") or 0) > 0
                or (seen.get("reviewDecision") or "REVIEW_REQUIRED")
                != "REVIEW_REQUIRED"
                or seen.get("state") in ("MERGED", "CLOSED")):
            entry["first_activity_day"] = today
        if seen.get("state") == "MERGED":
            entry["state"] = "MERGED"
            entry["merged_day"] = today
            repo = _repo_of(entry["pr"])
            outcomes.setdefault("repos", {}).setdefault(
                repo, {"merged_with_us": 0})["merged_with_us"] += 1
            transitions.append(f"{entry['pr']}: OPEN -> MERGED")
        elif seen.get("state") == "CLOSED":
            entry["state"] = "CLOSED"
            entry["closed_day"] = today
            transitions.append(f"{entry['pr']}: OPEN -> CLOSED (unmerged)")
    return outcomes, transitions


def _breaker_trip_repos(circuit=None):
    """Repos with a recorded circuit trip. Best-effort read: the breaker's own
    halt machinery guards unreadability; this is a belt-and-suspenders
    degradation trigger, not the breaker itself."""
    try:
        if circuit is None:
            circuit = json.load(open(CIRCUIT))
        return {_repo_of(t.get("pr", "")) for t in circuit.get("trips", [])
                if t.get("pr")}
    except Exception:
        return set()


def _stale_unresponsive(repo, monitor_state, stale_days):
    """True if EVERY open PR of ours in the repo has zero maintainer
    engagement and no activity for > stale_days (declining responsiveness,
    policy 4d). No open PRs = no signal = not stale."""
    live = monitor_state.get("state") or {}
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=stale_days)).isoformat()
    ours = [v for k, v in live.items()
            if _repo_of(k) == repo and v.get("state") == "OPEN"]
    if not ours:
        return False
    return all((v.get("n_reviews") or 0) == 0
               and (v.get("updatedAt") or "9999")[:10] < cutoff
               for v in ours)


def _policy_entry(repo, cfg):
    """The per-repo policy dict for `repo`, or a string describing why it is
    unusable. Returns (entry_or_None, error_or_None) — never raises."""
    policy = (cfg or {}).get("repo_policy")
    if policy is None:
        return None, None
    if not isinstance(policy, dict):
        return None, "executor.repo_policy is not an object"
    entry = policy.get(repo)
    if entry is None:
        return None, None
    if not isinstance(entry, dict):
        return None, f"executor.repo_policy[{repo!r}] is not an object"
    return entry, None


def policy_max_open(repo, cfg=None):
    """Per-repo hard ceiling from `executor.repo_policy`, or None.

    Malformed policy yields 0 (fail closed) rather than being ignored: a repo
    admitted under a restriction must not silently revert to the default limit
    because someone mistyped its config entry."""
    entry, err = _policy_entry(repo, {**DEFAULTS, **(cfg or {})})
    if err:
        return 0, err
    if not entry or entry.get("max_open") is None:
        return None, None
    try:
        return max(0, int(entry["max_open"])), None
    except (TypeError, ValueError):
        return 0, f"executor.repo_policy[{repo!r}].max_open is not an integer"


def autoship_allowed(repo, cfg=None):
    """Whether `repo` may be shipped to by the UNATTENDED auto-ship path.

    Returns (bool, reason). This is orthogonal to capacity: an operator-only
    repo still has a normal open-PR limit, it just requires a human to open
    each PR. Repos with no policy entry are allowed — `approved_repos` is the
    allowlist, this is a restriction on top of it — but a policy that cannot
    be parsed denies, because the whole point of an entry is to withhold
    autonomy from a repo that has not earned it yet."""
    entry, err = _policy_entry(repo, {**DEFAULTS, **(cfg or {})})
    if err:
        return False, f"{err} -> auto-ship denied (fail closed)"
    if not entry:
        return True, "no per-repo policy; auto-ship allowed"
    if entry.get("autoship") is False:
        note = entry.get("note") or "auto-ship withheld by per-repo policy"
        return False, f"operator-only repo: {note}"
    if entry.get("autoship") not in (True, None):
        return False, (f"executor.repo_policy[{repo!r}].autoship is not a bool "
                       "-> auto-ship denied (fail closed)")
    return True, "per-repo policy permits auto-ship"


def label_requirements(repo, cfg=None):
    """Per-repo issue-label requirements from `executor.repo_policy`.

    Returns (required, excluded, error). Some repos accept PRs only for
    issues in a specific label state (cli/cli: `help wanted` mandatory,
    `core` forbidden — their CONTRIBUTING says PRs for unlabelled or core
    issues will not be accepted). Malformed entries yield an error string,
    and every caller treats that as a denial — a repo admitted under a label
    restriction must not silently revert to unrestricted because someone
    mistyped its config entry."""
    entry, err = _policy_entry(repo, {**DEFAULTS, **(cfg or {})})
    if err:
        return None, None, err
    if not entry:
        return [], [], None
    out = []
    for key in ("required_labels", "excluded_labels"):
        v = entry.get(key)
        if v is None:
            v = []
        if not isinstance(v, list) or not all(
                isinstance(x, str) and x.strip() for x in v):
            return None, None, (f"executor.repo_policy[{repo!r}].{key} is not "
                                "a list of non-empty strings")
        out.append(v)
    return out[0], out[1], None


def issue_labels_allowed(repo, label_names, cfg=None):
    """Whether an issue's LIVE labels satisfy the repo's label policy.

    Returns (bool, reason). `label_names` must come from a fresh fetch, never
    from the stored queue copy — a label can be added or removed between
    ranking and acting, and the label state IS the repo's permission signal.
    Matching is case-insensitive on the whole label name. Fail closed on
    malformed policy or unreadable label input."""
    required, excluded, err = label_requirements(repo, cfg)
    if err:
        return False, f"{err} -> denied (fail closed)"
    if not required and not excluded:
        return True, "no per-repo label requirements"
    try:
        have = {str(name).strip().lower() for name in (label_names or [])}
    except TypeError:
        return False, "issue labels unreadable -> denied (fail closed)"
    missing = [x for x in required if x.strip().lower() not in have]
    if missing:
        return False, (f"required label(s) {missing} not on the live issue — "
                       "the repo's contribution process gates autonomous "
                       "action on this label state")
    hit = [x for x in excluded if x.strip().lower() in have]
    if hit:
        return False, (f"excluded label(s) {hit} present on the live issue — "
                       "the repo's contribution process rejects PRs here")
    return True, (f"label policy satisfied (required {required}, "
                  f"excluded {excluded})")


def path_allowlist(repo, cfg=None):
    """Per-repo auto-ship path fence from `executor.repo_policy`.

    Returns (patterns, declared, error). `declared` is the whole point: an
    ABSENT `autoship_path_allowlist` key means there is no fence, while a
    PRESENT-but-empty one means deny every patch. Collapsing those two would
    turn an armed-and-inert fence into no fence at all, which is exactly the
    state cosmos/cosmos-sdk is in today (design_autoship_path_fence_2026-10-08
    §3.1) — the fence is in place with an unratified, empty allowlist.

    An allowlist rather than a denylist because the property being fenced
    (cosmos's "different AppState given same genesisState and txList") has a
    large and unobvious reachable surface: a denylist fails SILENTLY when it is
    incomplete, an allowlist fails closed.
    """
    entry, err = _policy_entry(repo, {**DEFAULTS, **(cfg or {})})
    if err:
        return None, False, err
    if not entry or "autoship_path_allowlist" not in entry:
        return [], False, None
    v = entry["autoship_path_allowlist"]
    if not isinstance(v, list) or not all(
            isinstance(x, str) and x.strip() for x in v):
        return None, True, (f"executor.repo_policy[{repo!r}]."
                            "autoship_path_allowlist is not a list of "
                            "non-empty strings")
    return v, True, None


def _fence_regex(pattern):
    """Translate one fence pattern to a fully-anchored regex.

    Hand-rolled instead of `fnmatch` because `fnmatch`'s `*` crosses `/`:
    `client/*` would match `client/keeper/deep.go` and quietly admit a whole
    subtree the operator never listed. Here `**` crosses separators and a bare
    `*` does not, which is the only reading under which a listed pattern means
    what it looks like.
    """
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
            continue
        if c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _normalize_fence_path(f):
    """Repo-relative, `/`-separated path, or None if it is not one we will
    reason about. Absolute paths and `..` traversal return None and therefore
    deny: a fence that cannot tell where a file lives must not permit it."""
    if not isinstance(f, str) or not f.strip():
        return None
    p = f.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or ".." in p.split("/"):
        return None
    return p


def paths_allowed(repo, changed_files, cfg=None):
    """Whether every changed file falls inside the repo's auto-ship path fence.

    Returns (bool, reason). EVERY changed file is subject, tests and changelog
    included — an allowlist with an implicit carve-out is a denylist wearing an
    allowlist's name. A repo whose bundles touch CHANGELOG.md must list it.

    Unreadable or empty `changed_files` denies when a fence is declared:
    UNKNOWN is never PASS.
    """
    patterns, declared, err = path_allowlist(repo, cfg)
    if err:
        return False, f"{err} -> denied (fail closed)"
    if not declared:
        return True, "no per-repo auto-ship path fence"
    if not isinstance(changed_files, list) or not changed_files:
        return False, ("auto-ship path fence declared but changed_files is "
                       "empty or unreadable -> denied (fail closed)")
    if not patterns:
        return False, ("auto-ship path fence declared with an EMPTY allowlist "
                       "— no path has been ratified for the unattended path "
                       "on this repo yet")
    rx = [_fence_regex(p) for p in patterns]
    for f in changed_files:
        p = _normalize_fence_path(f)
        if p is None:
            return False, (f"changed file {f!r} is not a usable repo-relative "
                           "path -> denied (fail closed)")
        if not any(r.match(p) for r in rx):
            return False, (f"{p} is outside the auto-ship path fence "
                           f"{patterns} — this change class has not been "
                           "ratified for the unattended path on this repo")
    return True, (f"all {len(changed_files)} changed files inside the "
                  f"auto-ship path fence {patterns}")


def capacity(repo, monitor_state, outcomes, cfg=None, circuit=None):
    """Effective open-PR limit for a repo (repo-concurrency policy).
    Returns (int, [reasons]); reasons[0] always names the regime."""
    cfg = {**DEFAULTS, **(cfg or {})}
    absolute = cfg["absolute_max_open"]
    pol_max, pol_err = policy_max_open(repo, cfg)
    if pol_max is not None:
        absolute = min(absolute, pol_max)

    # close-unmerged cooldown: capacity 0, kept stricter than degraded-1
    today = datetime.date.today()
    for entry in outcomes.get("prs", []):
        if (_repo_of(entry["pr"]) == repo and entry.get("state") == "CLOSED"
                and entry.get("closed_day")):
            age = (today - datetime.date.fromisoformat(entry["closed_day"])).days
            if age < cfg["close_cooldown_days"]:
                return 0, [f"cooldown: {entry['pr']} closed unmerged {age}d "
                           f"ago — capacity 0 for {cfg['close_cooldown_days']}d"]

    # degradation triggers (policy 4) -> effective limit 1
    degraded = []
    _, changes_requested = _open_prs_by_repo(monitor_state, outcomes)
    if repo in changes_requested:
        degraded.append("unresolved CHANGES_REQUESTED on "
                        + ", ".join(changes_requested[repo]))
    live = monitor_state.get("state") or {}
    for entry in outcomes.get("prs", []):
        if (entry.get("shipped_by") == "autoship"
                and _repo_of(entry.get("pr", "")) == repo):
            state = (live.get(entry["pr"]) or {}).get("state") or entry.get("state")
            if state == "OPEN":
                degraded.append(f"unmerged auto-shipped {entry['pr']}")
                break
    if repo in _breaker_trip_repos(circuit):
        degraded.append("circuit-breaker trip recorded for this repo")
    if _stale_unresponsive(repo, monitor_state, cfg["stale_activity_days"]):
        degraded.append(f"no maintainer activity on any open PR for "
                        f"> {cfg['stale_activity_days']}d")
    if degraded:
        cap = min(cfg["degraded_max_open"], absolute)
        return cap, [f"degraded: limit {cap} until confidence recovers"] + degraded

    cap = min(cfg["default_max_open"], absolute)
    if pol_err:
        return cap, [f"restricted: limit {cap} open PRs ({pol_err} "
                     "-> treated as ceiling 0 for the policy clamp)"]
    if pol_max is not None:
        return cap, [f"restricted: limit {cap} open PRs "
                     f"(executor.repo_policy caps this repo at {pol_max})"]
    return cap, [f"default: limit {cap} open PRs "
                 "(third slot is operator-only with recorded justification; "
                 f"absolute ceiling {absolute})"]


def remaining(repo, monitor_state=None, outcomes=None, cfg=None, circuit=None):
    """Capacity minus live OPEN count. SHIPPING semantics: any read failure
    -> (0, reason). Returns (int, [reasons]).

    ``circuit``: pass the already-loaded circuit doc when the caller holds one
    (the executor does, state-dir aware); None falls back to the production
    circuit file, which is correct for production-path callers like gate 8."""
    try:
        if monitor_state is None:
            monitor_state = json.load(open(MONITOR_STATE))
        if outcomes is None:
            outcomes = load_outcomes()
    except Exception as e:
        return 0, [f"health data unreadable -> capacity 0 (fail closed): {e}"]
    try:
        cap, reasons = capacity(repo, monitor_state, outcomes, cfg, circuit)
        open_count, _ = _open_prs_by_repo(monitor_state, outcomes)
        n_open = open_count.get(repo, 0)
        return max(0, cap - n_open), reasons + [
            f"{n_open} open now (monitor state + ledger, incl. ships from today)"]
    except Exception as e:
        return 0, [f"health computation failed -> capacity 0 (fail closed): {e}"]


def capacity_map(monitor_state, outcomes, cfg=None, circuit=None):
    """Capacity per repo for every repo present in either input."""
    repos = set(outcomes.get("repos", {}))
    repos.update(_repo_of(k) for k in (monitor_state.get("state") or {}))
    out = {}
    for repo in sorted(repos):
        try:
            cap, reasons = capacity(repo, monitor_state, outcomes, cfg, circuit)
        except Exception as e:  # malformed ledger entry must not kill reports
            cap, reasons = 0, [f"capacity computation failed (fail closed): {e}"]
        out[repo] = {"capacity": cap, "reasons": reasons}
    return out


def scout_counts_or_fallback(scout_cfg):
    """Discovery-side adapter: (open_counts, cap_map). On ANY failure returns
    (legacy counts, {}) so scouting proceeds under the fixed fallback limit."""
    try:
        monitor_state = json.load(open(MONITOR_STATE))
        outcomes = load_outcomes()
        counts, _ = _open_prs_by_repo(monitor_state, outcomes)
        caps = {r: v["capacity"] for r, v in
                capacity_map(monitor_state, outcomes,
                             scout_cfg.get("executor")).items()}
        # approved repos with no history yet get base capacity, not the
        # looser fixed fallback — discovery stays consistent with ship-time
        for repo in (scout_cfg.get("issue_scout") or {}).get("approved_repos", []):
            if repo not in caps:
                caps[repo], _ = capacity(repo, monitor_state, outcomes,
                                         scout_cfg.get("executor"))
        return counts, caps
    except Exception:
        return None, {}


def breaker_should_trip(outcomes, monitor_state, acknowledged=()):
    """Trip reason if any AUTO-shipped PR was closed unmerged, else None.

    acknowledged: PR keys already recorded in circuit_state trips — an
    operator re-arm must not instantly re-trip on the same persisted closure
    (audit H4: the breaker is edge-triggered, not level-triggered)."""
    live = monitor_state.get("state") or {}
    for entry in outcomes.get("prs", []):
        if entry.get("shipped_by") != "autoship":
            continue
        if entry.get("pr") in acknowledged:
            continue
        seen = live.get(entry["pr"]) or {}
        state = seen.get("state") or entry.get("state")
        if state == "CLOSED":
            return f"auto-shipped {entry['pr']} closed unmerged", entry.get("pr")
    return None


def missing_autoship_keys(outcomes, monitor_state):
    """Auto-shipped ledger PRs still marked OPEN but ABSENT from the live
    monitor state — the breaker cannot observe their closure (audit H5).
    Callers must treat any result as a halt-worthy anomaly."""
    live = monitor_state.get("state") or {}
    return [entry["pr"] for entry in outcomes.get("prs", [])
            if entry.get("shipped_by") == "autoship"
            and entry.get("state") == "OPEN"
            and entry.get("pr") not in live]
