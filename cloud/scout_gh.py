"""Shared read-only GitHub access for the daily scout — gh CLI subprocess
helpers plus a hard API-call budget.

Same I/O philosophy as monitor_fetch.py: authenticated `gh` only, honest
failure (raise, never fabricate). Every network call spends the budget first,
so a runaway query set degrades the cycle instead of the operator's rate
limit. No writes to GitHub, ever.
"""

import json
import subprocess


class ScoutFetchError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ApiBudget:
    def __init__(self, max_calls):
        self.max_calls = max_calls
        self._used = 0

    def spend(self, n=1):
        if self._used + n > self.max_calls:
            raise BudgetExceeded(f"API budget exhausted ({self._used}/{self.max_calls})")
        self._used += n

    def used(self):
        return self._used


def gh_json(args, budget, timeout=60):
    """Run `gh <args>` and parse stdout as JSON. Spends 1 budget call."""
    budget.spend()
    p = subprocess.run(["gh", *args], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise ScoutFetchError((p.stderr or p.stdout or "gh failed").strip()[:200])
    try:
        return json.loads(p.stdout)
    except Exception:
        raise ScoutFetchError(f"unparseable gh output: {p.stdout.strip()[:120]}")


def gh_graphql(query, budget, timeout=60, **variables):
    """`gh api graphql` with typed variable flags (str -> -f, int -> -F)."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    data = gh_json(args, budget, timeout=timeout)
    if data.get("errors"):
        raise ScoutFetchError(f"graphql errors: {json.dumps(data['errors'])[:200]}")
    return data["data"]


def gh_search_repos(query, limit, budget):
    return gh_json(["search", "repos", *query.split(), "--limit", str(limit),
                    "--json", "fullName,stargazersCount,language,pushedAt,"
                    "isArchived,isFork,description"], budget)


def gh_reachable(budget):
    """Cheap probe: can we make an authenticated API call at all?"""
    try:
        gh_json(["api", "rate_limit"], budget, timeout=30)
        return True
    except (ScoutFetchError, BudgetExceeded, OSError, subprocess.TimeoutExpired):
        return False


_BOTS = ("claassistant", "cla-assistant", "codecov", "codecov-commenter",
         "github-actions", "dependabot", "netlify", "vercel", "sonarcloud")


def is_bot(login):
    lg = (login or "").lower()
    return "[bot]" in lg or lg.endswith("-app") or lg in _BOTS


def is_maintainer(assoc):
    return (assoc or "").upper() in ("OWNER", "MEMBER", "COLLABORATOR")
