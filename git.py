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
import tempfile, subprocess, tarfile, io
from pathlib import Path
import asyncio, json

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from glob import glob

# --------- Config & utilitaires ----------
GITHUB_API = "https://api.github.com"
USER_AGENT = "github-analytics-ref/1.0 (+https://github.com)"
LOG = logging.getLogger("github_analytics")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# --- Cache disque très simple + suivi de job ---
CACHE_DIR = Path("./cache"); CACHE_DIR.mkdir(exist_ok=True)
_jobs: dict[str, str] = {}  # owner -> "idle" | "running" | "done" | "error"

def _cache_path(owner: str) -> Path:
    return CACHE_DIR / f"overview_{owner}.json"

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _pct(part: float, total: float) -> float:
    return round((part * 100.0 / total), 1) if total else 0.0

def _ts() -> str:
    # 2025-08-23T14-05-31Z → sûr pour nom de fichier
    return datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")

def _versioned_path(owner: str) -> Path:
    return CACHE_DIR / f"overview_{owner}_{_ts()}.json"

def _latest_path(owner: str) -> Path:
    return CACHE_DIR / f"overview_{owner}_latest.json"

def write_cached(owner: str, data: dict) -> Path:
    # Écrit un fichier versionné + met à jour "latest" (copie)
    ver = _versioned_path(owner)
    lat = _latest_path(owner)
    payload = json.dumps(data, ensure_ascii=False)
    ver.write_text(payload, encoding="utf-8")
    lat.write_text(payload, encoding="utf-8")
    return ver

def read_cached(owner: str) -> Optional[dict]:
    p = _latest_path(owner)
    if not p.exists(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

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
    
    def languages_loc_aggregate(self, owner: str) -> List[Dict[str, Any]]:
        """
        Calcule les LOC par langage en téléchargeant les tarballs et en exécutant 'cloc'.
        Si 'cloc' est absent ou renvoie vide, on bascule sur un scanner LOC interne.
        """
        repos = self.get_all_repos(owner)
        agg_loc: Dict[str, int] = {}
        used_fallback = False

        for r in repos:
            name = r.get("name")
            if not name:
                continue
            default_branch = r.get("default_branch") or "main"
            tar_bytes = self._download_repo_tarball(owner, name, default_branch)
            if not tar_bytes:
                continue

            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
                        tf.extractall(tmpdir)

                    # 1) Tentative avec cloc
                    cloc_json = self._cloc_directory(tmpdir)
                    if cloc_json:
                        for lang, info in cloc_json.items():
                            if not isinstance(info, dict):
                                continue
                            if lang in ("header", "SUM"):
                                continue
                            code_loc = int(info.get("code", 0))
                            if code_loc > 0:
                                agg_loc[lang] = agg_loc.get(lang, 0) + code_loc
                    else:
                        # 2) Fallback scanner interne
                        used_fallback = True
                        scan = self._simple_loc_scan(root)
                        for lang, cnt in scan.items():
                            agg_loc[lang] = agg_loc.get(lang, 0) + cnt

            except Exception:
                # tar corrompu / extraction impossible → on ignore ce repo
                continue

        # Petit log pour t'aider à diagnostiquer
        if not agg_loc:
            LOG.warning("languages_loc_aggregate: aucun LOC détecté. "
                        "Vérifie que 'cloc' est installé (choco/scoop/winget sur Windows) "
                        "ou que le fallback lit bien les sources.")

        return [{"language": k, "loc": v} for k, v in sorted(agg_loc.items(), key=lambda kv: kv[1], reverse=True)]

    # --- GitHub Actions (30 derniers jours) --
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
    
    def commits_by_month(self, owner: str, months: int = 12) -> List[Dict[str, Any]]:
        """
        Agrège tous les commits (branche par défaut) par mois (clé = 'month_end': YYYY-MM-28/30/31).
        Fenêtre glissante 'months' mois.
        """
        end = _now_utc()
        # approx: 31 jours * months (fenêtre large)
        since = end - timedelta(days=31 * months)
        buckets: Dict[tuple, int] = {}  # key = (year, month)

        repos = self.get_all_repos(owner)
        for r in repos:
            name = r.get("name")
            if not name:
                continue
            default_branch = r.get("default_branch") or "main"
            page = 1
            while True:
                commits = self.client.safe_get(
                    f"repos/{owner}/{name}/commits",
                    params={"sha": default_branch, "since": since.isoformat(), "per_page": 100, "page": page}
                )
                if not isinstance(commits, list) or not commits:
                    break
                for c in commits:
                    commit = c.get("commit", {})
                    author = commit.get("author") or {}
                    date_str = author.get("date")
                    if not date_str:
                        continue
                    dt = isoparse(date_str).astimezone(timezone.utc)
                    k = (dt.year, dt.month)
                    buckets[k] = buckets.get(k, 0) + 1
                if len(commits) < 100:
                    break
                page += 1

        # Construire série ordonnée, combler les mois manquants à 0
        series = []
        # curseur au 1er du plus ancien mois attendu
        start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
        cursor = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
        last = datetime(end.year, end.month, 1, tzinfo=timezone.utc)

        while cursor <= last:
            y, m = cursor.year, cursor.month
            # fin de mois = mois suivant - 1 jour
            if m == 12:
                month_end = datetime(y + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
            else:
                month_end = datetime(y, m + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
            series.append({
                "month_end": month_end.date().isoformat(),
                "count": buckets.get((y, m), 0)
            })
            # mois suivant
            if m == 12:
                cursor = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
            else:
                cursor = datetime(y, m + 1, 1, tzinfo=timezone.utc)

        return series


    # --- Releases (compte simple) ---
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

        # Localisation des langages
        languages_loc = self.languages_loc_aggregate(owner)  # peut être [] si cloc absent
        top_lang_loc = languages_loc[0] if languages_loc else {"language": None, "loc": 0}
        total_lang_loc = sum(l["loc"] for l in languages_loc) or 0
        top_lang_loc_share = _pct(top_lang_loc["loc"], total_lang_loc) if total_lang_loc else 0.0

        # Actions CI (30 derniers jours)
        actions = self.actions_summary(owner, days=30, per_repo_pages=3)

        # Top repos par stars
        topstars = self.top_repos_by_stars(owner, top_n=8)

        # Releases
        releases = self.releases_count(owner)

        # Commits par semaine (12 dernières semaines) + total
        commits12w = self.commits_by_week(owner, weeks=12)
        commits12w_total = sum(int(x.get("count", 0)) for x in commits12w)

        # MAINTENANT : commits par mois (12 derniers mois)
        commits12m = self.commits_by_month(owner, months=12)
        commits12m_total = sum(int(x.get("count", 0)) for x in commits12m)


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
                "commits12mTotal": commits12m_total,
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
            "commitsByMonth": commits12m,
            "fetchedAt": _now_utc().isoformat(),
            "languagesLoc": languages_loc,
            "kpisLoc": {
                "topLanguage": {
                    "name": top_lang_loc["language"],
                    "loc": top_lang_loc["loc"],
                    "sharePct": top_lang_loc_share
                },
                "totalLoc": total_lang_loc
            }
        }
    
    def _download_repo_tarball(self, owner: str, repo_name: str, ref: str = None) -> bytes | None:
        """
        Télécharge le tarball du repo (branche par défaut si ref=None).
        Retourne les octets du .tar.gz ou None en cas d'erreur.
        """
        # Ex: GET /repos/{owner}/{repo}/tarball/{ref}
        path = f"repos/{owner}/{repo_name}/tarball"
        if ref:
            path += f"/{ref}"
        try:
            resp = self.client.session.get(f"{GITHUB_API}/{path}", timeout=60, headers=self.client.session.headers)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception:
            return None


    def _cloc_directory(self, dir_path: str) -> dict:
        """
        Lance cloc sur un dossier et retourne le JSON parsé.
        """
        try:
            # --json pour sortie JSON ; --quiet pour réduire le bruit
            proc = subprocess.run(
                ["cloc", dir_path, "--json", "--quiet"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, text=True
            )
            if proc.returncode not in (0, 1):  # cloc renvoie parfois 1 si aucun fichier compatible
                return {}
            import json
            data = json.loads(proc.stdout or "{}")
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            # cloc non trouvé
            return {}
        except Exception:
            return {}
        
        # ----------------- Fallback LOC scanner (si cloc indisponible) -----------------

    _IGNORE_DIRS = {
        ".git", ".hg", ".svn", ".venv", "venv", "env",
        "node_modules", "dist", "build", "out", "coverage",
        "bin", "obj", "target", ".next", ".nuxt", ".cache"
    }

    _EXT_LANG = {
        ".ts": "TypeScript", ".tsx": "TypeScript",
        ".js": "JavaScript", ".jsx": "JavaScript",
        ".py": "Python",
        ".cs": "C#",
        ".java": "Java",
        ".css": "CSS",
        ".scss": "SCSS",
        ".c": "C", ".h": "C",
        ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
        ".hpp": "C++", ".hh": "C++", ".hxx": "C++",
        ".dart": "Dart",
        ".cmake": "CMake",
        ".sh": "Shell", ".bash": "Shell",
        ".swift": "Swift",
        ".kt": "Kotlin", ".kts": "Kotlin",
        ".m": "Objective-C", ".mm": "Objective-C",
        ".html": "HTML", ".htm": "HTML",
        ".yml": "YAML", ".yaml": "YAML",
        ".mk": "Makefile",
        ".dockerfile": "Dockerfile",
    }

    _SPECIAL_BASENAMES = {
        "Dockerfile": "Dockerfile",
        "Makefile": "Makefile",
        "Procfile": "Procfile",
        "CMakeLists.txt": "CMake",
    }

    def _guess_lang_from_path(self, path: Path) -> Optional[str]:
        name = path.name
        if name in self._SPECIAL_BASENAMES:
            return self._SPECIAL_BASENAMES[name]
        # Dockerfile.* → Dockerfile
        if name.lower().startswith("dockerfile"):
            return "Dockerfile"
        # CMakeLists.* → CMake
        if name.startswith("CMakeLists"):
            return "CMake"
        return self._EXT_LANG.get(path.suffix.lower())

    def _is_text_file(self, path: Path) -> bool:
        try:
            with path.open("rb") as f:
                chunk = f.read(4096)
            # binaire si présence de NUL
            return b"\x00" not in chunk
        except Exception:
            return False

    def _count_loc_in_file(self, path: Path, lang: str) -> int:
        """
        Compte lignes de code “simples” : ignore vides + commentaires ligne (#, //, --)
        et gère grossièrement les commentaires bloc C-style /* ... */ et HTML <!-- ... -->.
        Ce n’est pas parfait, mais suffisant comme fallback.
        """
        single_hash = {"Python", "Shell", "Makefile", "CMake", "YAML", "Dockerfile", "Procfile"}
        single_slash = {"TypeScript", "JavaScript", "C#", "Java", "C", "C++", "CSS", "SCSS", "Kotlin", "Swift", "Objective-C", "Dart"}
        html_like = {"HTML"}

        loc = 0
        in_block = False
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue

                    # HTML block <!-- ... -->
                    if lang in html_like:
                        if "<!--" in line and "-->" not in line:
                            in_block = True
                        if in_block:
                            if "-->" in line:
                                in_block = False
                            continue
                        if line.startswith("<!--") or line.endswith("-->"):
                            continue

                    # C-style block /* ... */
                    if lang in single_slash | {"CSS", "SCSS"}:
                        if "/*" in line and "*/" not in line:
                            in_block = True
                        if in_block:
                            if "*/" in line:
                                in_block = False
                            continue

                    if lang in single_hash and (line.startswith("#")):
                        continue
                    if lang in single_slash and (line.startswith("//")):
                        continue
                    if lang == "SQL" and line.startswith("--"):
                        continue

                    loc += 1
        except Exception:
            return 0
        return loc

    def _simple_loc_scan(self, root: Path) -> Dict[str, int]:
        agg: Dict[str, int] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            # ignore dossiers connus
            dirnames[:] = [d for d in dirnames if d not in self._IGNORE_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                lang = self._guess_lang_from_path(p)
                if not lang:
                    continue
                if not self._is_text_file(p):
                    continue
                cnt = self._count_loc_in_file(p, lang)
                if cnt > 0:
                    agg[lang] = agg.get(lang, 0) + cnt
        return agg



# --------- API FASTAPI (facultatif mais pratique) ----------
app = FastAPI(title="GitHub Analytics Ref", version="1.0.0")

# ----- CORS: lire depuis l'env -----
raw_allowed = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5271,https://localhost:7271"
)
ALLOWED_ORIGINS = [o.strip() for o in raw_allowed.split(",") if o.strip()]


# ⚠️ Ajoute TOUTES les origines que tu utilises en dev.
"""ALLOWED_ORIGINS = [
    "http://localhost:5271",   # ton Blazor WASM en dev (port à adapter)
    "https://localhost:7271",  # si tu lances Blazor en HTTPS
    "http://localhost:5000", "https://localhost:5001",  # autres ports habituels
    "http://127.0.0.1:5271",
]"""

"""async def _recompute_overview(owner: str):
    try:
        _jobs[owner] = "running"
        ga = _ga()  # utilise ton helper existant
        data = ga.overview_user(owner)  # ⚠️ potentiellement long (170s)
        write_cached(owner, data)
        _jobs[owner] = "done"
    except Exception:
        LOG.exception("recompute overview failed")
        _jobs[owner] = "error"
        """

async def _recompute_overview(owner: str):
    try:
        _jobs[owner] = "running"
        _status_meta[owner] = {"status":"running", "startedAt": _now_utc().isoformat()}
        ga = _ga()
        data = ga.overview_user(owner)          # long
        path = write_cached(owner, data)        # <-- écrit versionné + latest
        _jobs[owner] = "done"
        _status_meta[owner].update({"status":"done", "updatedAt": _now_utc().isoformat(), "file": str(path.name)})
    except Exception as e:
        LOG.exception("recompute overview failed")
        _jobs[owner] = "error"
        _status_meta[owner] = {"status":"error", "error": str(e), "updatedAt": _now_utc().isoformat()}



app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,   # pas besoin, le token reste côté serveur Python
    allow_methods=["*"],
    allow_headers=["*"],
)

def _ga() -> GitHubAnalytics:
    return GitHubAnalytics(token=os.getenv("GITHUB_TOKEN"))

"""@app.get("/overview")
def api_overview(owner: str = Query(..., description="User ou Organisation GitHub")):
    # 1) servir le cache si dispo
    cached = read_cached(owner)
    if cached:
        return cached

    # 2) pas de cache → lancer un build si pas déjà en cours
    if _jobs.get(owner) != "running":
        asyncio.create_task(_recompute_overview(owner))
        _jobs[owner] = "running"

    # 3) indiquer au client d’attendre et de repoller
    # (tu peux aussi renvoyer {"status":"building"} avec 202)
    from fastapi import Response
    raise HTTPException(status_code=202, detail="Overview en cours de construction. Réessaie dans quelques secondes.")"""

# ----- Health check simple -----
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/overview/refresh")
async def api_overview_refresh(owner: str):
    # si un job tourne déjà, ne pas relancer
    if _jobs.get(owner) == "running":
        return {"status": "running"}

    # lance le recalcul en tâche de fond et répond tout de suite
    asyncio.create_task(_recompute_overview(owner))
    return {"status": "started"}

"""@app.get("/overview/status")
def api_overview_status(owner: str):
    return {"status": _jobs.get(owner, "idle")}"""
_status_meta: dict[str, dict] = {}  # owner -> {startedAt, updatedAt, status, error?}

async def _recompute_overview(owner: str):
    try:
        _jobs[owner] = "running"
        _status_meta[owner] = {"status":"running", "startedAt": _now_utc().isoformat()}
        ga = _ga()
        data = ga.overview_user(owner)
        write_cached(owner, data)
        _jobs[owner] = "done"
        _status_meta[owner].update({"status":"done", "updatedAt": _now_utc().isoformat()})
    except Exception as e:
        LOG.exception("recompute overview failed")
        _jobs[owner] = "error"
        _status_meta[owner] = {"status":"error", "error": str(e), "updatedAt": _now_utc().isoformat()}

"""@app.get("/overview/status")
def api_overview_status(owner: str):
    return _status_meta.get(owner, {"status": _jobs.get(owner, "idle")})

@app.get("/overview")
def api_overview(owner: str = Query(..., description="User ou Organisation GitHub")):
    try:
        return _ga().overview_user(owner)
    except HTTPException as e:
        raise e
    except Exception as e:
        LOG.exception("overview error")
        raise HTTPException(status_code=500, detail=str(e))"""

# (facultatif) exposer le dossier /cache pour accès direct
app.mount("/cache", StaticFiles(directory=str(CACHE_DIR), html=False), name="cache")

@app.get("/overview/cached")
def api_overview_cached(owner: str):
    """Retourne le JSON 'latest' s’il existe, sinon 404."""
    data = read_cached(owner)
    if data is None:
        raise HTTPException(status_code=404, detail="No cached overview yet.")
    return JSONResponse(content=data)

@app.get("/overview/file")
def api_overview_file(owner: str):
    """Téléchargement du 'latest'."""
    path = _latest_path(owner)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No cached file.")
    return FileResponse(path, media_type="application/json", filename=path.name)

@app.get("/overview/files")
def api_overview_files(owner: str):
    """Liste les fichiers versionnés disponibles (ordre desc)."""
    pattern = str(CACHE_DIR / f"overview_{owner}_*.json")
    files = sorted(glob(pattern), reverse=True)
    return [{"file": Path(f).name, "size": Path(f).stat().st_size} for f in files]

@app.post("/overview/rebuild")
async def api_overview_rebuild(owner: str):
    """Lance un recalcul qui écrira un nouveau fichier + mettra à jour latest."""
    if _jobs.get(owner) == "running":
        return {"status": "running"}
    asyncio.create_task(_recompute_overview(owner))
    return {"status": "started"}

@app.get("/overview/status")
def api_overview_status(owner: str):
    return _status_meta.get(owner, {"status": _jobs.get(owner, "idle")})

# (option) si tu veux que GET /overview fasse aussi une écriture à chaque appel :
@app.get("/overview")
def api_overview(owner: str = Query(..., description="User/Org GitHub")):
    ga = _ga()
    data = ga.overview_user(owner)
    write_cached(owner, data)   # <-- écrit à chaque requête
    return JSONResponse(content=data)

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

@app.get("/languages_loc")
def api_languages_loc(owner: str):
    try:
        return _ga().languages_loc_aggregate(owner)
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
    
@app.get("/commits_by_month")
def api_commits_by_month(owner: str, months: int = 12):
    try:
        return _ga().commits_by_month(owner, months=months)
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
