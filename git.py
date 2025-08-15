"""
github_analytics.py — Agrégations analytiques GitHub (module + API FastAPI)

Dépendances:
  pip install requests fastapi uvicorn python-dateutil

Auth:
  - Token personnel GitHub (recommandé) via env GITHUB_TOKEN
  - Sans token: limité à ~60 req/h (risque de 403 rate limit)
Usage:
  - Module:
      from github_analytics import GitHubAnalytics
      ga = GitHubAnalytics(token=os.getenv("GITHUB_TOKEN"))
      data = ga.overview_user("dioprawane")
  - API:
      uvicorn github_analytics:app --host 0.0.0.0 --port 8000
      GET /overview?owner=dioprawane
"""

from __future__ import annotations
import os, time, math, sys, logging
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timedelta, timezone
import requests
from dateutil.parser import isoparse
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# --------- Config & utilitaires ----------
GITHUB_API = "https://api.github.com"
USER_AGENT = "github-analytics-ref/1.0 (+https://github.com)"
LOG = logging.getLogger("github_analytics")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pct(part: float, total: float) -> float:
    return round((part * 100.0 / total), 1) if total else 0.0


class GitHubClient:
    def __init__(self, token: Optional[str] = None, per_page: int = 100, sleep_on_rate=True):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.per_page = per_page
        self.sleep_on_rate = sleep_on_rate

    def _check_rate(self, resp: requests.Response):
        # Si proche de la limite, temporiser prudemment
        remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
        reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
        if remaining <= 1 and self.sleep_on_rate:
            delay = max(0, reset_at - int(time.time()) + 1)
            LOG.warning("Rate limit proche (remaining=%s). Pause %ss…", remaining, delay)
            time.sleep(delay)

    def get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        url = f"{GITHUB_API.rstrip('/')}/{path.lstrip('/')}"
        r = self.session.get(url, params=params, timeout=30)
        self._check_rate(r)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise HTTPException(status_code=429, detail="GitHub rate limit exceeded")
        r.raise_for_status()
        return r.json()

    def paginate(self, path: str, params: Dict[str, Any] | None = None, max_pages: int = 100) -> List[Any]:
        """Paginer proprement (jusqu’à max_pages)."""
        params = dict(params or {})
        params.setdefault("per_page", self.per_page)
        page = 1
        results: List[Any] = []
        while page <= max_pages:
            params["page"] = page
            data = self.get(path, params=params)
            if not isinstance(data, list):
                # Certains endpoints renvoient { items: [...] }
                items = data.get("items") if isinstance(data, dict) else None
                if isinstance(items, list):
                    data = items
                else:
                    break
            results.extend(data)
            if len(data) < params["per_page"]:
                break
            page += 1
        return results

    def safe_get(self, path: str, params: dict | None = None):
        try:
            return self.get(path, params=params)
        except HTTPException as e:
            if e.status_code in (403, 404, 410):
                return None
            raise
        except Exception:
            return None

    def safe_paginate(self, path: str, params: dict | None = None, max_pages: int = 100) -> list:
        try:
            return self.paginate(path, params=params, max_pages=max_pages)
        except HTTPException as e:
            if e.status_code in (403, 404, 410):
                return []
            raise
        except Exception:
            return []


# --------- Couche "analytics" ----------
class GitHubAnalytics:
    """def __init__(self, token: Optional[str] = None):
        self.client = GitHubClient(token=token)"""
    def __init__(self, token: Optional[str] = None):
        self.client = GitHubClient(token=token)
        # Ex: GITHUB_SKIP_REPOS="federated_learning,foo,bar*"
        raw = os.getenv("GITHUB_SKIP_REPOS", "")
        self.skip_patterns = [p.strip() for p in raw.split(",") if p.strip()]

    def _should_skip(self, repo_name: str) -> bool:
        if not repo_name:
            return True
        if not self.skip_patterns:
            return False
        import fnmatch
        return any(fnmatch.fnmatch(repo_name, pat) for pat in self.skip_patterns)

    # --- Repos & stars ---
    """def get_all_repos(self, owner: str) -> List[Dict[str, Any]]:
        # NOTE: pour un user -> /users/:owner/repos ; pour une org -> /orgs/:owner/repos
        # On essaie user d’abord puis org en fallback.
        try:
            return self.client.paginate(f"users/{owner}/repos", params={"type": "all", "sort": "updated"})
        except HTTPException:
            return self.client.paginate(f"orgs/{owner}/repos", params={"type": "all", "sort": "updated"})"""
    def get_all_repos(self, owner: str) -> List[Dict[str, Any]]:
        try:
            repos = self.client.paginate(f"users/{owner}/repos", params={"type": "all", "sort": "updated"})
        except HTTPException:
            repos = self.client.paginate(f"orgs/{owner}/repos", params={"type": "all", "sort": "updated"})

        # Filtre: ignorer les repos à problème
        filtered = []
        for r in repos:
            name = r.get("name") or ""
            if self._should_skip(name):
                continue
            if r.get("archived") or r.get("disabled"):
                continue
            # (optionnel) ignorer les forks:
            # if r.get("fork"):
            #     continue
            filtered.append(r)
        return filtered

    def top_repos_by_stars(self, owner: str, top_n: int = 8) -> List[Dict[str, Any]]:
        repos = self.get_all_repos(owner)
        sorted_repos = sorted(repos, key=lambda r: int(r.get("stargazers_count", 0)), reverse=True)
        return [
            {"name": r.get("name"), "stars": int(r.get("stargazers_count", 0))}
            for r in sorted_repos[:top_n]
        ]

    def repo_visibility_breakdown(self, repos: List[Dict[str, Any]]) -> Dict[str, int]:
        public_count = sum(1 for r in repos if (r.get("visibility") == "public") or (r.get("private") is False))
        total = len(repos)
        return {
            "total": total,
            "public": public_count,
            "private": max(0, total - public_count)
        }

    def total_stars(self, repos: List[Dict[str, Any]]) -> int:
        return sum(int(r.get("stargazers_count", 0)) for r in repos)

    # --- Languages agrégés ---
    def languages_aggregate(self, owner: str) -> List[Dict[str, Any]]:
        repos = self.get_all_repos(owner)
        agg: Dict[str, int] = {}
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            try:
                langs = self.client.get(f"repos/{owner}/{name}/languages")
            except Exception:
                continue
            for lang, bytes_count in (langs or {}).items():
                agg[lang] = agg.get(lang, 0) + int(bytes_count)
        # Tri desc
        return [{"language": k, "bytes": v} for k, v in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)]

    # --- GitHub Actions (30 derniers jours) ---
    """ef actions_summary(self, owner: str, days: int = 30, per_repo_pages: int = 3) -> Dict[str, Any]:
        since = _now_utc() - timedelta(days=days)
        repos = self.get_all_repos(owner)

        workflows_total = 0
        runs_total = 0
        success = 0
        failure = 0

        for r in repos:
            repo_name = r.get("name")
            if not repo_name:
                continue

            # Workflows count
            try:
                wf = self.client.get(f"repos/{owner}/{repo_name}/actions/workflows")
                workflows_total += int(wf.get("total_count", 0))
            except Exception:
                pass

            # Runs: paginer un peu pour rester raisonnable
            page = 1
            while page <= per_repo_pages:
                runs_res = self.client.get(
                    f"repos/{owner}/{repo_name}/actions/runs",
                    params={"per_page": 100, "page": page}
                )
                runs = runs_res.get("workflow_runs") or []
                if not runs:
                    break

                for run in runs:
                    created_at = isoparse(run.get("created_at"))
                    if created_at < since:
                        # Les runs sont classés desc : on peut sortir tôt pour ce repo
                        break
                    runs_total += 1
                    conclusion = (run.get("conclusion") or "").lower()
                    if conclusion == "success":
                        success += 1
                    elif conclusion == "failure":
                        failure += 1

                if len(runs) < 100:
                    break
                page += 1

        pass_rate = round((success * 100.0 / runs_total), 1) if runs_total else 0.0
        return {
            "workflows": workflows_total,
            "runs": runs_total,
            "success": success,
            "failure": failure,
            "passRate": pass_rate,
            "windowDays": days
        }"""
    def actions_summary(self, owner: str, days: int = 30, per_repo_pages: int = 3) -> Dict[str, Any]:
        from fastapi import HTTPException
        since = _now_utc() - timedelta(days=days)
        repos = self.get_all_repos(owner)

        workflows_total = 0
        runs_total = 0
        success = 0
        failure = 0

        for r in repos:
            repo_name = r.get("name")
            if not repo_name:
                continue

            # --- Workflows count (skip si 404/403) ---
            try:
                wf = self.client.get(f"repos/{owner}/{repo_name}/actions/workflows")
                workflows_total += int(wf.get("total_count", 0))
            except HTTPException as e:
                if e.status_code in (403, 404):
                    # Actions désactivées / accès refusé: on ignore ce repo
                    continue
                else:
                    # autre erreur: on passe simplement ce repo
                    continue
            except Exception:
                continue

            # --- Runs (paginés) ---
            page = 1
            while page <= per_repo_pages:
                try:
                    runs_res = self.client.get(
                        f"repos/{owner}/{repo_name}/actions/runs",
                        params={"per_page": 100, "page": page}
                    )
                except HTTPException as e:
                    if e.status_code in (403, 404):
                        # pas d'Actions pour ce repo -> on sort de la boucle repo
                        break
                    else:
                        break
                except Exception:
                    break

                runs = runs_res.get("workflow_runs") or []
                if not runs:
                    break

                stop_early = False
                for run in runs:
                    created_at = isoparse(run.get("created_at"))
                    if created_at < since:
                        # runs triés desc: on peut arrêter tôt pour ce repo
                        stop_early = True
                        break
                    runs_total += 1
                    conclusion = (run.get("conclusion") or "").lower()
                    if conclusion == "success":
                        success += 1
                    elif conclusion == "failure":
                        failure += 1

                if stop_early or len(runs) < 100:
                    break
                page += 1

        pass_rate = round((success * 100.0 / runs_total), 1) if runs_total else 0.0
        return {
            "workflows": workflows_total,
            "runs": runs_total,
            "success": success,
            "failure": failure,
            "passRate": pass_rate,
            "windowDays": days
        }

    # --- Issues & PR ---
    """def issues_count(self, owner: str, state: str = "all") -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            issues = self.client.paginate(
                f"repos/{owner}/{name}/issues",
                params={"state": state, "filter": "all"},
                max_pages=5,  # limiter raisonnablement
            )
            # NB: l'endpoint retourne aussi les PRs; filtrer sur "pull_request" absent
            total += sum(1 for i in issues if "pull_request" not in i)
        return total

    def pull_requests_count(self, owner: str, state: str = "all") -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            prs = self.client.paginate(
                f"repos/{owner}/{name}/pulls",
                params={"state": state},
                max_pages=5,
            )
            total += len(prs)
        return total"""

    def issues_count(self, owner: str, state: str = "all") -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            try:
                issues = self.client.paginate(
                    f"repos/{owner}/{name}/issues",
                    params={"state": state, "filter": "all"},
                    max_pages=5,
                )
                # L'endpoint renvoie aussi les PR -> ne compter que les issues pures
                total += sum(1 for i in issues if "pull_request" not in i)
            except HTTPException as e:
                if e.status_code in (403, 404, 410):
                    # Issues désactivées / accès refusé -> on ignore ce repo
                    continue
                else:
                    continue
            except Exception:
                continue
        return total

    def pull_requests_count(self, owner: str, state: str = "all") -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            try:
                prs = self.client.paginate(
                    f"repos/{owner}/{name}/pulls",
                    params={"state": state},
                    max_pages=5,
                )
                total += len(prs)
            except HTTPException as e:
                if e.status_code in (403, 404, 410):
                    # PRs désactivées / accès refusé -> on ignore
                    continue
                else:
                    continue
            except Exception:
                continue
        return total


    # --- Commits par semaine (tous repos, fenêtre glissante) ---
    def commits_by_week(self, owner: str, weeks: int = 12) -> List[Dict[str, Any]]:
        """
        Renvoie une série {week_end (ISO date), count} en agrégeant tous les repos.
        Note: on parcourt l'historique récent avec 'since' pour limiter les coûts.
        """
        end = _now_utc()
        since = end - timedelta(weeks=weeks)
        buckets: Dict[str, int] = {}  # key = samedi fin de semaine (iso date)

        repos = self.get_all_repos(owner)
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            # Pour limiter: on ne parcourt que la branche par défaut
            default_branch = r.get("default_branch") or "main"
            page = 1
            while True:
                """commits = self.client.get(
                    f"repos/{owner}/{name}/commits",
                    params={"sha": default_branch, "since": since.isoformat(), "per_page": 100, "page": page}
                )
                if not isinstance(commits, list) or not commits:
                    break"""
                commits = self.client.safe_get(
                    f"repos/{owner}/{name}/commits",
                    params={"sha": default_branch, "since": since.isoformat(), "per_page": 100, "page": page}
                )
                if not isinstance(commits, list) or not commits:
                    break
                for c in commits:
                    # date = commit.author.date (peut différer de committer)
                    commit = c.get("commit", {})
                    author = commit.get("author") or {}
                    date_str = author.get("date")
                    if not date_str:
                        continue
                    dt = isoparse(date_str).astimezone(timezone.utc)
                    # Bucket fin de semaine = samedi
                    diff = (5 - dt.weekday()) % 7  # 5 = samedi
                    week_end = (dt + timedelta(days=diff)).date().isoformat()
                    buckets[week_end] = buckets.get(week_end, 0) + 1
                if len(commits) < 100:
                    break
                page += 1

        # Normaliser la série sur toutes les semaines demandées
        series = []
        cursor = since
        while cursor <= end:
            diff = (5 - cursor.weekday()) % 7
            week_end = (cursor + timedelta(days=diff)).date().isoformat()
            series.append({"week_end": week_end, "count": buckets.get(week_end, 0)})
            cursor += timedelta(weeks=1)
        # Fusionner doublons potentiels par clé
        from collections import OrderedDict
        tmp = OrderedDict()
        for s in series:
            tmp[s["week_end"]] = tmp.get(s["week_end"], 0) + s["count"]
        return [{"week_end": k, "count": v} for k, v in tmp.items()]

    # --- Releases (compte simple) ---
    """def releases_count(self, owner: str) -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            rels = self.client.paginate(f"repos/{owner}/{name}/releases", max_pages=3)
            total += len(rels)
        return total"""
    def releases_count(self, owner: str) -> int:
        repos = self.get_all_repos(owner)
        total = 0
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            rels = self.client.safe_paginate(f"repos/{owner}/{name}/releases", max_pages=3)
            total += len(rels)
        return total
    
    def collaborators_split(self, owner: str) -> dict:
        """
        Retourne le nombre de dépôts 'solo' (1 seul contributeur) vs 'multi'.
        On interroge /contributors avec per_page=2 (early-exit) pour limiter les quotas.
        - solo: exactement 1 contributeur
        - multi: >= 2 contributeurs
        - unknown: erreur d'accès/404, etc. (on l'exclut du %)
        """
        repos = self.get_all_repos(owner)
        solo = multi = unknown = 0

        for r in repos:
            name = r.get("name")
            if not name:
                continue

            # per_page=2 suffit pour savoir si >1
            resp = self.client.safe_get(
                f"repos/{owner}/{name}/contributors",
                params={"per_page": 2, "anon": 1}
            )

            if resp is None:
                unknown += 1
                continue

            # resp est une liste (ou vide)
            try:
                count = len(resp)
            except Exception:
                unknown += 1
                continue

            if count == 0:
                # repo vide → on considère 'solo' (créé par toi)
                solo += 1
            elif count == 1:
                solo += 1
            else:
                multi += 1

        known = solo + multi
        return {
            "solo": solo,
            "multi": multi,
            "unknown": unknown,
            "soloSharePct": _pct(solo, known) if known else 0.0,
            "known": known
        }

    # --- Vue "overview" consolidée (aligne avec ton front Blazor) ---
    """def overview_user(self, owner: str) -> Dict[str, Any]:
        repos = self.get_all_repos(owner)
        vis = self.repo_visibility_breakdown(repos)
        languages = self.languages_aggregate(owner)
        actions = self.actions_summary(owner, days=30, per_repo_pages=3)
        topstars = self.top_repos_by_stars(owner, top_n=8)
        issues_all = self.issues_count(owner, state="all")
        prs_all = self.pull_requests_count(owner, state="all")
        releases = self.releases_count(owner)
        commits12w = self.commits_by_week(owner, weeks=12)

        return {
            "owner": owner,
            "kpis": {
                "repos": vis["total"],
                "publicRepos": vis["public"],
                "privateRepos": vis["private"],
                "totalStars": self.total_stars(repos),
                "releases": releases,
                "issues_all": issues_all,
                "prs_all": prs_all,
            },
            "actions30d": actions,
            "languages": languages,                     # [{language, bytes}...]
            "topStars": topstars,                        # [{name, stars}...]
            "commitsByWeek": commits12w,                 # [{week_end, count}...]
            "fetchedAt": _now_utc().isoformat(),
        }"""

    # --- Vue "overview" MINIMALE (sans issues/labels/contribs) ---
    def overview_user(self, owner: str) -> Dict[str, Any]:
        # Repos + visibilités + stars
        repos = self.get_all_repos(owner)
        vis = self.repo_visibility_breakdown(repos)
        total_stars = self.total_stars(repos)

        # Langages (octets agrégés)
        languages = self.languages_aggregate(owner)
        top_lang = languages[0] if languages else {"language": None, "bytes": 0}
        total_lang_bytes = sum(l["bytes"] for l in languages) or 0
        top_lang_share = _pct(top_lang["bytes"], total_lang_bytes) if total_lang_bytes else 0.0

        # Actions CI (30 derniers jours)
        actions = self.actions_summary(owner, days=30, per_repo_pages=3)

        # Top repos par stars
        topstars = self.top_repos_by_stars(owner, top_n=8)

        # Releases
        releases = self.releases_count(owner)

        # Commits par semaine (12 dernières semaines) + total
        commits12w = self.commits_by_week(owner, weeks=12)
        commits12w_total = sum(int(x.get("count", 0)) for x in commits12w)

        # 🔹 Split solo vs multi
        collab = self.collaborators_split(owner)

        return {
            "owner": owner,
            "kpis": {
                "repos": vis["total"],
                "publicRepos": vis["public"],
                "privateRepos": vis["private"],
                "totalStars": total_stars,
                "releases": releases,
                "commits12wTotal": commits12w_total,
                "topLanguage": {
                    "name": top_lang["language"],
                    "bytes": top_lang["bytes"],
                    "sharePct": top_lang_share
                }
            },
            "collab": collab,                   # 👈 nouveau bloc
            "actions30d": actions,
            "languages": languages,
            "topStars": topstars,
            "commitsByWeek": commits12w,
            "fetchedAt": _now_utc().isoformat()
        }


# --------- API FASTAPI (facultatif mais pratique) ----------
app = FastAPI(title="GitHub Analytics Ref", version="1.0.0")

# ⚠️ Ajoute TOUTES les origines que tu utilises en dev.
ALLOWED_ORIGINS = [
    "http://localhost:5271",   # ton Blazor WASM en dev (port à adapter)
    "https://localhost:7271",  # si tu lances Blazor en HTTPS
    "http://localhost:5000", "https://localhost:5001",  # autres ports habituels
    "http://127.0.0.1:5271",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,   # pas besoin, le token reste côté serveur Python
    allow_methods=["*"],
    allow_headers=["*"],
)

def _ga() -> GitHubAnalytics:
    return GitHubAnalytics(token=os.getenv("GITHUB_TOKEN"))

@app.get("/overview")
def api_overview(owner: str = Query(..., description="User ou Organisation GitHub")):
    try:
        return _ga().overview_user(owner)
    except HTTPException as e:
        raise e
    except Exception as e:
        LOG.exception("overview error")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/repos")
def api_repos(owner: str):
    try:
        return _ga().get_all_repos(owner)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/languages")
def api_languages(owner: str):
    try:
        return _ga().languages_aggregate(owner)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/actions")
def api_actions(owner: str, days: int = 30):
    try:
        return _ga().actions_summary(owner, days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/commits_by_week")
def api_commits(owner: str, weeks: int = 12):
    try:
        return _ga().commits_by_week(owner, weeks=weeks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------- Entrée ligne de commande ----------
if __name__ == "__main__":
    import json
    owner = sys.argv[1] if len(sys.argv) > 1 else "dioprawane"
    ga = GitHubAnalytics(token=os.getenv("GITHUB_TOKEN"))
    print("GITHUB_TOKEN", os.getenv("GITHUB_TOKEN"))
    result = ga.overview_user(owner)
    print(json.dumps(result, ensure_ascii=False, indent=2))
