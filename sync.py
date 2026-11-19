#!/usr/bin/env python3
"""Keep murikbmx LineageOS forks in sync with upstream.

1. Reads the murikbmx/android manifest (default.xml + snippets/*.xml) and
   collects every murikbmx/* project.
2. For forks whose GitHub parent is LineageOS/*: merge-upstream on the
   tracked branch (no force, merge commit / fast-forward only).
3. For forks that have a `gmscompat` branch: merge lineage-24.0 into it
   (server-side merge, 409 on conflict).
4. Watches the AOSP tag LineageOS pins in its manifest; when it changes,
   the AOSP-based gmscompat forks (no GitHub parent) need a manual rebase.
5. One issue per problem (label `sync-conflict`), updated on repeat,
   closed automatically once the problem is gone. Job summary table.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

OWNER = "murikbmx"
MANIFEST_REPO = f"{OWNER}/android"
BRANCH = os.environ.get("SYNC_BRANCH", "lineage-24.0")
GMS_BRANCH = "gmscompat"
UPSTREAM_OWNER = "LineageOS"
DRY_RUN = os.environ.get("DRY_RUN", "false") == "true"
DO_GMS = os.environ.get("SYNC_GMSCOMPAT", "true") == "true"
SELF_REPO = os.environ["GITHUB_REPOSITORY"]
LABEL = "sync-conflict"

FORK_TOKEN = os.environ["FORK_SYNC_TOKEN"]
ISSUE_TOKEN = os.environ["GITHUB_TOKEN"]


def api(method, path, token=FORK_TOKEN, body=None, ok=(200, 201, 204)):
    """Returns (status, json). Retries transient errors."""
    url = path if path.startswith("http") else f"https://api.github.com/{path}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"message": raw.decode(errors="replace")[:300]}
            if e.code in (500, 502, 503, 504) or (
                e.code == 403 and "rate limit" in str(payload).lower()
            ):
                time.sleep(10 * (attempt + 1))
                continue
            return e.code, payload
        except urllib.error.URLError:
            time.sleep(10 * (attempt + 1))
    return 599, {"message": "network error after retries"}


def manifest_file(path, ref=BRANCH):
    st, js = api("GET", f"repos/{MANIFEST_REPO}/contents/{path}?ref={ref}")
    if st != 200:
        return None
    return base64.b64decode(js["content"]).decode()


def collect_projects():
    files = ["default.xml"]
    st, listing = api("GET", f"repos/{MANIFEST_REPO}/contents/snippets?ref={BRANCH}")
    if st == 200:
        files += [f["path"] for f in listing if f["name"].endswith(".xml")]
    projects, default_rev, aosp_tag = {}, BRANCH, None
    for f in files:
        text = manifest_file(f)
        if not text:
            continue
        root = ET.fromstring(text)
        d = root.find("default")
        if d is not None and f == "default.xml":
            default_rev = d.get("revision", BRANCH).replace("refs/heads/", "")
        for rem in root.findall("remote"):
            if rem.get("name") == "aosp" and rem.get("revision"):
                aosp_tag = rem.get("revision").replace("refs/tags/", "")
        for p in root.findall("project"):
            name = p.get("name", "")
            if not name.startswith(f"{OWNER}/"):
                continue
            repo = name.split("/", 1)[1]
            rev = (p.get("revision") or default_rev).replace("refs/heads/", "")
            projects.setdefault(repo, set()).add(rev)
    # projects that exist only on the gmscompat manifest branch
    text = manifest_file("default.xml", GMS_BRANCH)
    if text:
        for p in ET.fromstring(text).findall("project"):
            name = p.get("name", "")
            if name.startswith(f"{OWNER}/") and GMS_BRANCH in (p.get("revision") or ""):
                projects.setdefault(name.split("/", 1)[1], set()).add(GMS_BRANCH)
    return projects, aosp_tag


def upstream_aosp_tag():
    st, js = api(
        "GET", f"repos/{UPSTREAM_OWNER}/android/contents/default.xml?ref={BRANCH}"
    )
    if st != 200:
        return None
    m = re.search(
        r'name="aosp".*?revision="refs/tags/([^"]+)"',
        base64.b64decode(js["content"]).decode(),
        re.S,
    )
    return m.group(1) if m else None


# ---------------------------------------------------------------- issues
_issues = None


def open_issues():
    global _issues
    if _issues is None:
        _issues = {}
        page = 1
        while True:
            st, js = api(
                "GET",
                f"repos/{SELF_REPO}/issues?state=open&labels={LABEL}&per_page=100&page={page}",
                token=ISSUE_TOKEN,
            )
            if st != 200 or not js:
                break
            for i in js:
                _issues[i["title"]] = i["number"]
            page += 1
    return _issues


def report_problem(title, body):
    if DRY_RUN:
        return
    issues = open_issues()
    if title in issues:
        api(
            "POST",
            f"repos/{SELF_REPO}/issues/{issues[title]}/comments",
            token=ISSUE_TOKEN,
            body={"body": f"Still failing.\n\n{body}"},
        )
    else:
        st, js = api(
            "POST",
            f"repos/{SELF_REPO}/issues",
            token=ISSUE_TOKEN,
            body={"title": title, "body": body, "labels": [LABEL]},
        )
        if st == 201:
            issues[title] = js["number"]


def resolve_problem(title):
    if DRY_RUN:
        return
    issues = open_issues()
    if title in issues:
        n = issues.pop(title)
        api(
            "POST",
            f"repos/{SELF_REPO}/issues/{n}/comments",
            token=ISSUE_TOKEN,
            body={"body": "Resolved: the sync now succeeds."},
        )
        api(
            "PATCH",
            f"repos/{SELF_REPO}/issues/{n}",
            token=ISSUE_TOKEN,
            body={"state": "closed", "state_reason": "completed"},
        )


# ---------------------------------------------------------------- sync
def compare(repo, base, head):
    st, js = api("GET", f"repos/{OWNER}/{repo}/compare/{base}...{head}")
    return js if st == 200 else None


def sync_upstream(repo, parent, branch):
    title = f"{repo}: {branch} conflicts with {parent}"
    if DRY_RUN:
        st, js = api(
            "GET",
            f"repos/{parent}/compare/{branch}...{OWNER}:{repo}:{branch}",
        )
        if st != 200:
            return "error", js.get("message", st)
        return ("dry-run", f"behind by {js['behind_by']}")
    st, js = api(
        "POST", f"repos/{OWNER}/{repo}/merge-upstream", body={"branch": branch}
    )
    if st == 200:
        resolve_problem(title)
        kind = js.get("merge_type", "none")
        return ("up to date" if kind == "none" else kind), js.get("message", "")
    if st == 409:
        po, pr = parent.split("/")
        cmp = compare(repo, branch, f"{po}:{pr}:{branch}")
        behind = cmp["ahead_by"] if cmp else "?"
        report_problem(
            title,
            f"`merge-upstream` returned a conflict for `{OWNER}/{repo}` "
            f"branch `{branch}` ({behind} upstream commits not merged).\n\n"
            "Resolve locally:\n```\n"
            f"git fetch https://github.com/{parent} {branch}\n"
            "git merge FETCH_HEAD   # fix conflicts\n"
            f"git push github HEAD:{branch}\n```",
        )
        return "CONFLICT", f"{behind} commits pending"
    return "error", f"{st} {js.get('message', '')}"


def sync_gmscompat(repo):
    title = f"{repo}: {BRANCH} → {GMS_BRANCH} merge conflict"
    cmp = compare(repo, GMS_BRANCH, BRANCH)
    if cmp is None:
        return "error", "compare failed"
    if cmp["ahead_by"] == 0:
        resolve_problem(title)
        return "up to date", ""
    if DRY_RUN:
        return "dry-run", f"{cmp['ahead_by']} commits to merge"
    st, js = api(
        "POST",
        f"repos/{OWNER}/{repo}/merges",
        body={
            "base": GMS_BRANCH,
            "head": BRANCH,
            "commit_message": f"Merge branch '{BRANCH}' into {GMS_BRANCH}",
        },
    )
    if st in (201, 204):
        resolve_problem(title)
        return "merged", f"{cmp['ahead_by']} commits"
    if st == 409:
        report_problem(
            title,
            f"Merging `{BRANCH}` into `{GMS_BRANCH}` of `{OWNER}/{repo}` "
            f"conflicts ({cmp['ahead_by']} commits).\n\n```\n"
            f"git checkout {GMS_BRANCH}\n"
            f"git merge github/{BRANCH}   # fix conflicts\n"
            f"git push github HEAD:{GMS_BRANCH}\n```",
        )
        return "CONFLICT", f"{cmp['ahead_by']} commits pending"
    return "error", f"{st} {js.get('message', '')}"


def main():
    projects, our_tag = collect_projects()
    rows, failed, parents = [], False, {}

    for repo in sorted(projects):
        st, info = api("GET", f"repos/{OWNER}/{repo}")
        if st != 200:
            rows.append((repo, "-", "error", f"repo lookup {st}"))
            failed = True
            continue
        parent = (info.get("parent") or {}).get("full_name")
        parents[repo] = parent
        st, _ = api("GET", f"repos/{OWNER}/{repo}/branches/{GMS_BRANCH}")
        has_gms = st == 200

        for branch in sorted(projects[repo]):
            if branch == GMS_BRANCH:
                continue
            if not parent or not parent.startswith(f"{UPSTREAM_OWNER}/"):
                rows.append((repo, branch, "skipped", f"parent: {parent or 'none'}"))
                continue
            if branch != BRANCH:
                rows.append((repo, branch, "skipped", "not the sync branch"))
                continue
            res, note = sync_upstream(repo, parent, branch)
            rows.append((repo, branch, res, note))
            failed |= res in ("CONFLICT", "error")

        if has_gms and DO_GMS and parent and parent.startswith(f"{UPSTREAM_OWNER}/"):
            res, note = sync_gmscompat(repo)
            rows.append((repo, f"{BRANCH}→{GMS_BRANCH}", res, note))
            failed |= res in ("CONFLICT", "error")

    # AOSP tag watch (AOSP-based gmscompat forks have no GitHub parent)
    up_tag = upstream_aosp_tag()
    aosp_forks = [
        r for r in projects if GMS_BRANCH in projects[r] and not parents.get(r)
    ]
    tag_title = "AOSP tag changed: rebase AOSP-based gmscompat forks"
    if up_tag and our_tag and up_tag != our_tag:
        report_problem(
            tag_title,
            f"LineageOS now pins `{up_tag}`, our manifest pins `{our_tag}`.\n\n"
            "Merge the new tag into `gmscompat` of: "
            + ", ".join(f"`{r}`" for r in sorted(aosp_forks)),
        )
        rows.append(("AOSP tag", "-", "CHANGED", f"{our_tag} → {up_tag}"))
    elif up_tag and our_tag:
        resolve_problem(tag_title)
        rows.append(("AOSP tag", "-", "up to date", up_tag))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [
        f"### Fork sync {'(dry run)' if DRY_RUN else ''}",
        "",
        "| Repo | Branch | Result | Note |",
        "|---|---|---|---|",
    ]
    order = {"CONFLICT": 0, "error": 0, "CHANGED": 1, "merged": 2,
             "merge": 2, "fast-forward": 2, "dry-run": 3}
    for r in sorted(rows, key=lambda x: (order.get(x[2], 5), x[0])):
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    text = "\n".join(lines)
    print(text)
    if summary:
        with open(summary, "a") as fh:
            fh.write(text + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
