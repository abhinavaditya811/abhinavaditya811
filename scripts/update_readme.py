#!/usr/bin/env python3
"""Regenerate the auto-updated blocks in README.md from the past week of GitHub activity.

Reads GitHub contribution data, optionally asks Claude to write a short narrative
summary, and splices the result between HTML comment markers in README.md.

Environment:
  GITHUB_TOKEN       required. A PAT with `repo` read scope surfaces private work;
                     the default Actions token only sees public activity.
  GITHUB_USER        required. The login to summarize.
  NARRATIVE_BACKEND  optional. "cli" (default when the `claude` binary is present,
                     uses your Claude subscription), "api" (needs ANTHROPIC_API_KEY),
                     or "none" to skip the narrative entirely.
  ANTHROPIC_API_KEY  optional. Only used by the "api" backend.
  README_PATH        optional. Defaults to README.md next to this script's repo root.

scripts/projects.json holds hand-written blurbs the narrative blends with the raw
activity. Without an entry a project can only be described from commit subjects,
which reads thin, so add one when you start something new.

Private repositories are aggregated as counts only -- their names, URLs, and commit
messages never leave this process and are never sent to Claude by either backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com"
ACTIVITY_MARKERS = ("<!-- ACTIVITY:START -->", "<!-- ACTIVITY:END -->")
CURRENT_MARKERS = ("<!-- CURRENT:START -->", "<!-- CURRENT:END -->")

# Languages are aggregated across public and private repos. A language name is not
# identifying the way a repo name is; flip this to False to count public repos only.
INCLUDE_PRIVATE_LANGUAGES = True

# How many public repos to pull commit subjects for, to give the narrative some texture.
DETAIL_REPO_LIMIT = 3

MODEL = "claude-opus-5"
CLI_MODEL = "opus"  # `claude --model` alias. "fable" is not on the subscription
                    # plan and silently falls back to Opus, so ask for Opus directly.
CLI_TIMEOUT = 300


# --------------------------------------------------------------------------- GitHub

def gh_request(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "readme-activity-bot")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


EXTERNAL_REPOS_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositoriesContributedTo(
      first: 100
      includeUserRepositories: false
      privacy: PUBLIC
      contributionTypes: [COMMIT, PULL_REQUEST, PULL_REQUEST_REVIEW, ISSUE]
    ) {
      totalCount
      nodes { nameWithOwner url stargazerCount }
    }
  }
}
"""

GRAPHQL_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      restrictedContributionsCount
      commitContributionsByRepository(maxRepositories: 50) {
        repository { nameWithOwner url isPrivate primaryLanguage { name } }
        contributions { totalCount }
      }
      pullRequestContributionsByRepository(maxRepositories: 50) {
        repository { nameWithOwner url isPrivate }
        contributions(first: 20) {
          totalCount
          nodes { pullRequest { number title url } }
        }
      }
      pullRequestReviewContributionsByRepository(maxRepositories: 50) {
        repository { nameWithOwner url isPrivate }
        contributions { totalCount }
      }
    }
  }
}
"""


def fetch_contributions(login: str, token: str, since: datetime, until: datetime) -> dict:
    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {
            "login": login,
            "from": since.isoformat().replace("+00:00", "Z"),
            "to": until.isoformat().replace("+00:00", "Z"),
        },
    }
    result = gh_request(f"{GITHUB_API}/graphql", token, method="POST", body=payload)
    if "errors" in result:
        raise RuntimeError(f"GitHub GraphQL error: {result['errors']}")
    return result["data"]["user"]["contributionsCollection"]


_external_repo_cache: dict = {}


def count_external_repos(login: str, token: str) -> dict:
    """Standing tally of repos the user contributed to but does not own.

    Cached: build_activity is called a second time when the window widens, and this
    figure does not depend on the window.
    """
    if login in _external_repo_cache:
        return _external_repo_cache[login]
    try:
        result = gh_request(
            f"{GITHUB_API}/graphql",
            token,
            method="POST",
            body={"query": EXTERNAL_REPOS_QUERY, "variables": {"login": login}},
        )
        data = result["data"]["user"]["repositoriesContributedTo"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TypeError,
            json.JSONDecodeError):
        _external_repo_cache[login] = {"total": 0, "top": []}
        return _external_repo_cache[login]

    nodes = sorted(data["nodes"], key=lambda n: -n.get("stargazerCount", 0))
    tally = {
        "total": data["totalCount"],
        "top": [
            {"name": n["nameWithOwner"], "url": n["url"], "stars": n.get("stargazerCount", 0)}
            for n in nodes[:3]
        ],
    }
    _external_repo_cache[login] = tally
    return tally


def fetch_commit_subjects(repo: str, login: str, token: str, since: datetime) -> list[str]:
    """Recent commit subjects for a public repo, newest first. Best-effort."""
    url = (
        f"{GITHUB_API}/repos/{repo}/commits"
        f"?author={login}&since={since.isoformat().replace('+00:00', 'Z')}&per_page=10"
    )
    try:
        commits = gh_request(url, token)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return []
    subjects = []
    for commit in commits:
        subject = commit.get("commit", {}).get("message", "").split("\n")[0].strip()
        # Skip merge noise and bot churn -- they say nothing about the week.
        if subject and not subject.startswith("Merge "):
            subjects.append(subject)
    return subjects


def fetch_public_events(login: str, token: str, since: datetime) -> dict[str, dict]:
    """Public activity from the events feed, keyed by repo.

    contributionsCollection only counts commits that land on a default branch, and it
    misses some org and fork activity entirely. The events feed catches those, at the
    cost of being public-only and capped at ~300 events / 90 days.
    """
    found: dict[str, dict] = {}
    for page in range(1, 4):
        try:
            events = gh_request(
                f"{GITHUB_API}/users/{login}/events/public?per_page=100&page={page}", token
            )
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            break
        if not events:
            break
        for event in events:
            created = datetime.strptime(event["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if created < since:
                return found
            name = event["repo"]["name"]
            row = found.setdefault(
                name,
                {
                    "name": name,
                    "url": f"https://github.com/{name}",
                    "commits": 0,
                    "pushes": 0,
                    "prs": [],
                    "reviews": 0,
                },
            )
            kind = event["type"]
            payload = event.get("payload", {})
            if kind == "PushEvent":
                # GitHub slimmed this payload -- `size` is often absent now. Count what
                # we can verify (pushes) rather than inventing a commit number.
                size = payload.get("distinct_size")
                if size is None:
                    size = payload.get("size")
                if size is None:
                    size = len(payload.get("commits") or [])
                row["commits"] += size
                row["pushes"] += 1
            elif kind == "PullRequestEvent" and payload.get("action") == "opened":
                pr = payload.get("pull_request", {})
                row["prs"].append(
                    {
                        "number": pr.get("number"),
                        "title": pr.get("title", ""),
                        "url": pr.get("html_url", row["url"]),
                    }
                )
            elif kind in ("PullRequestReviewEvent", "PullRequestReviewCommentEvent"):
                row["reviews"] += 1
    return found


# ------------------------------------------------------------------------ shaping

def build_activity(contrib: dict, login: str, token: str, since: datetime) -> dict:
    """Collapse the GraphQL response into public repo rows plus a private aggregate."""
    public: dict[str, dict] = {}
    private_commits = 0
    private_repos: set[str] = set()
    languages: dict[str, int] = {}

    def row(repo_node: dict) -> dict:
        name = repo_node["nameWithOwner"]
        return public.setdefault(
            name,
            {
                "name": name,
                "url": repo_node["url"],
                "commits": 0,
                "pushes": 0,
                "prs": [],
                "reviews": 0,
            },
        )

    for entry in contrib["commitContributionsByRepository"]:
        repo_node = entry["repository"]
        count = entry["contributions"]["totalCount"]
        language = (repo_node.get("primaryLanguage") or {}).get("name")
        if repo_node["isPrivate"]:
            private_commits += count
            private_repos.add(repo_node["nameWithOwner"])
            if language and INCLUDE_PRIVATE_LANGUAGES:
                languages[language] = languages.get(language, 0) + count
            continue
        row(repo_node)["commits"] += count
        if language:
            languages[language] = languages.get(language, 0) + count

    for entry in contrib["pullRequestContributionsByRepository"]:
        repo_node = entry["repository"]
        if repo_node["isPrivate"]:
            private_repos.add(repo_node["nameWithOwner"])
            continue
        target = row(repo_node)
        for node in entry["contributions"]["nodes"]:
            pr = node["pullRequest"]
            target["prs"].append({"number": pr["number"], "title": pr["title"], "url": pr["url"]})

    for entry in contrib["pullRequestReviewContributionsByRepository"]:
        repo_node = entry["repository"]
        if repo_node["isPrivate"]:
            private_repos.add(repo_node["nameWithOwner"])
            continue
        row(repo_node)["reviews"] += entry["contributions"]["totalCount"]

    # Fill gaps contributionsCollection doesn't cover. GraphQL stays authoritative for
    # repos it already reported, so merged-branch commits aren't counted twice.
    for name, row in fetch_public_events(login, token, since).items():
        if name in public:
            existing = public[name]
            existing["reviews"] = max(existing["reviews"], row["reviews"])
            existing["pushes"] = max(existing["pushes"], row["pushes"])
            known = {p["number"] for p in existing["prs"]}
            existing["prs"] += [p for p in row["prs"] if p["number"] not in known]
        elif row["commits"] or row["pushes"] or row["prs"] or row["reviews"]:
            public[name] = row

    # Drop the profile repo itself: most of its commits are this script's own
    # refreshes, and "I committed to my README" is not activity worth publishing.
    public.pop(f"{login}/{login}", None)
    for name in [n for n in public if n.lower() == f"{login.lower()}/{login.lower()}"]:
        public.pop(name)

    owner_prefix = f"{login.lower()}/"
    for row in public.values():
        row["external"] = not row["name"].lower().startswith(owner_prefix)

    repos = sorted(
        public.values(),
        key=lambda r: (r["commits"], r["pushes"], len(r["prs"]), r["reviews"]),
        reverse=True,
    )

    for repo in repos[:DETAIL_REPO_LIMIT]:
        repo["subjects"] = fetch_commit_subjects(repo["name"], login, token, since)

    # restrictedContributionsCount covers private work a token without `repo` scope
    # can't itemize. When the token *can* see it, the itemized count is authoritative.
    hidden = contrib.get("restrictedContributionsCount", 0)
    return {
        "repos": repos,
        "external_repos": count_external_repos(login, token),
        "private_commits": private_commits or hidden,
        "private_repo_count": len(private_repos),
        "languages": [lang for lang, _ in sorted(languages.items(), key=lambda kv: -kv[1])],
        "totals": {
            "commits": contrib["totalCommitContributions"],
            "prs": contrib["totalPullRequestContributions"],
            "reviews": contrib["totalPullRequestReviewContributions"],
        },
    }


def load_projects() -> dict:
    path = Path(__file__).resolve().parent / "projects.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"projects.json unreadable ({exc}); continuing without blurbs", file=sys.stderr)
        return {}


def is_empty(activity: dict) -> bool:
    return not activity["repos"] and not activity["private_commits"]


# Below this much public signal in the window, the section reads as an empty week
# even when it technically has a row. Widening beats publishing "1 push".
MIN_PUBLIC_SIGNAL = 3


def public_signal(activity: dict) -> int:
    return sum(
        r["commits"] + r["pushes"] + len(r["prs"]) + r["reviews"] for r in activity["repos"]
    )


def is_thin(activity: dict) -> bool:
    return public_signal(activity) < MIN_PUBLIC_SIGNAL


# --------------------------------------------------------------------------- Claude

def redact(activity: dict) -> dict:
    """Everything the model is allowed to see. No private repo names or messages."""
    return {
        "public_repos": [
            {
                "name": r["name"],
                "external": r["external"],
                "commits": r["commits"],
                "pushes": r["pushes"],
                "pull_requests": [p["title"] for p in r["prs"]],
                "reviews": r["reviews"],
                "recent_commit_subjects": r.get("subjects", [])[:6],
            }
            for r in activity["repos"]
        ],
        "open_source_all_time_NOT_this_window": activity["external_repos"],
        "private_work": {
            "commits": activity["private_commits"],
            "repo_count": activity["private_repo_count"],
        },
        "languages": activity["languages"],
        "totals": activity["totals"],
    }


SYSTEM_PROMPT = """You write the auto-updated activity blurb on a software engineer's \
GitHub profile README. You are given a machine-readable summary of their GitHub \
contributions for a time window.

Write in the engineer's own first-person voice: plain, concrete, a little understated. \
No hype words ("excited to share", "diving deep", "leveraging"), no emoji, no hashtags.

Never use em dashes or en dashes. Use commas, colons, parentheses, or separate \
sentences instead.

Rules:
- Ground every claim in the data given. Never invent a project, technology, or outcome.
- Commit subjects are terse and sometimes cryptic; generalize rather than quoting \
anything you don't understand.
- Private work is deliberately anonymized. Refer to it only in aggregate \
("a few private repos"), never guess what it is.
- If the window is quiet, say so briefly instead of padding.
- Contributions to repositories the engineer does not own are open source work and \
matter more than routine commits to their own projects. Whenever the data contains any, \
name those repositories and say what the contribution was (a fix, a review, a feature). \
Do not bury them behind personal-project work."""


def build_prompt(payload: dict, window_label: str, current_sentence: str,
                 projects: dict) -> str:
    return f"""Activity for {window_label}:

```json
{json.dumps(payload, indent=2)}
```

Author-written context for these projects (`project_context`):

```json
{json.dumps(projects, indent=2)}
```

`open_source_all_time_NOT_this_window` is a standing lifetime total, not activity \
from this window. Do not describe those repositories as something worked on during \
the window, and do not mention them at all unless they also appear in \
`public_repos`.

The README's About Me currently carries this "right now" paragraph:

    {current_sentence}

Produce two things:

1. `narrative`: 2-3 sentences summarizing what this window of work was actually about. \
Lead with the dominant thread.

2. `current_paragraph`: a fresh "what I'm building right now" paragraph for the About \
Me section. Two or three sentences, 90 words maximum. Density matters more than \
completeness here: pick the sharpest detail about each project and drop the rest. Lead with whatever the activity shows is the dominant \
project. Describe what it *is* and what is interesting about it, not what this week's \
commits touched: the reader wants to know the work, not the changelog.

Rules for `current_paragraph`:
- Use `project_context` for the substance. Those blurbs are author-written and \
authoritative; prefer them over anything you infer from commit subjects.
- Bold each project name and link it to its `url`, in markdown: **[Coo](url)**.
- Only name a project that appears in this window's `public_repos`. If the dominant \
work is private, say so and name whatever public project is also active, rather than \
inventing detail.
- If nothing public is active at all, return the previous paragraph unchanged.
- Do not mention week counts, commit counts, or dates. This paragraph sits in a \
biography, not a changelog; the activity section below already carries the numbers."""


def parse_json_blob(text: str) -> dict:
    """The CLI returns free text, so tolerate code fences and surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def ask_claude_cli(prompt: str) -> dict | None:
    """Headless Claude Code. Uses the local subscription login -- no API key."""
    binary = shutil.which("claude")
    if not binary:
        print("`claude` not found on PATH; skipping narrative", file=sys.stderr)
        return None

    cmd = [
        binary, "-p", prompt,
        "--output-format", "json",
        "--model", CLI_MODEL,
        "--effort", "high",
        # This is a pure text transform: no tools, and none of the default
        # Claude Code system prompt (which would be ~12k wasted tokens a run).
        "--restricted",
        "--system-prompt", SYSTEM_PROMPT + "\n\nRespond with raw JSON only -- no code "
        "fences, no commentary. Keys: `narrative` (string), `current_paragraph` (string).",
        "--disallowedTools", "Read", "Write", "Edit", "Glob", "Grep",
        "WebFetch", "WebSearch", "TodoWrite", "Task",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT)
    if proc.returncode != 0:
        print(f"claude exited {proc.returncode}: {proc.stderr[:400]}", file=sys.stderr)
        return None

    envelope = json.loads(proc.stdout)
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        print(f"claude returned an error envelope: {envelope.get('subtype')}", file=sys.stderr)
        return None
    return parse_json_blob(envelope["result"])


def ask_claude_api(prompt: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY unset; skipping narrative", file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed; skipping narrative", file=sys.stderr)
        return None

    user_prompt = prompt
    client = anthropic.Anthropic(api_key=api_key)
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        output_config={
            "effort": "low",
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "narrative": {"type": "string"},
                        "current_paragraph": {"type": "string"},
                    },
                    "required": ["narrative", "current_paragraph"],
                    "additionalProperties": False,
                },
            },
        },
    )

    if response.stop_reason == "refusal":
        print("Model declined to summarize; falling back to the plain list", file=sys.stderr)
        return None

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    return json.loads(text)


def ask_claude(payload: dict, window_label: str, current_sentence: str,
               projects: dict) -> dict | None:
    backend = os.environ.get("NARRATIVE_BACKEND")
    if not backend:
        backend = "cli" if shutil.which("claude") else "api"
    if backend == "none":
        return None
    prompt = build_prompt(payload, window_label, current_sentence, projects)
    return ask_claude_cli(prompt) if backend == "cli" else ask_claude_api(prompt)


# -------------------------------------------------------------------------- render

def format_window(since: datetime, until: datetime) -> str:
    if since.year == until.year:
        return f"{since:%b %-d} to {until:%b %-d, %Y}"
    return f"{since:%b %-d, %Y} to {until:%b %-d, %Y}"


def repo_line(repo: dict) -> str:
    bits = []
    if repo["commits"]:
        bits.append(f"{repo['commits']} commit{'s' if repo['commits'] != 1 else ''}")
    elif repo["pushes"]:
        bits.append(f"{repo['pushes']} push{'es' if repo['pushes'] != 1 else ''}")
    if repo["prs"]:
        bits.append(f"{len(repo['prs'])} PR{'s' if len(repo['prs']) != 1 else ''}")
    if repo["reviews"]:
        bits.append(f"{repo['reviews']} review{'s' if repo['reviews'] != 1 else ''}")

    detail = ""
    subjects = repo.get("subjects") or []
    if subjects:
        detail = f" · *{subjects[0]}*"
    elif repo["prs"]:
        detail = f" · *{repo['prs'][0]['title']}*"
    return f"- **[{repo['name']}]({repo['url']})**: {', '.join(bits)}{detail}"


def render_block(activity: dict, narrative: str | None, since: datetime,
                 until: datetime, widened: bool) -> str:
    heading = "Recent Activity" if widened else "What I'm Working On"
    lines = [f"## 🗓️ {heading}", "", f"*{format_window(since, until)}*", ""]

    if is_empty(activity):
        lines += [
            "Heads-down offline this stretch, nothing pushed publicly.",
            "",
            f"<sub>Updated automatically, last run {until:%b %-d, %Y}</sub>",
        ]
        return "\n".join(lines)

    if narrative:
        lines += [narrative, ""]

    own = [r for r in activity["repos"] if not r["external"]]
    external = [r for r in activity["repos"] if r["external"]]

    if own:
        lines += ["**My projects**", ""]
        lines += [repo_line(r) for r in own]
        lines.append("")

    if external:
        lines += ["**Open source and other repos**", ""]
        lines += [repo_line(r) for r in external]
        lines.append("")

    if activity["private_commits"]:
        count = activity["private_commits"]
        repos = activity["private_repo_count"]
        where = (
            f" across {repos} private repo{'s' if repos != 1 else ''}"
            if repos else " in private repos"
        )
        lines += [f"**Private work**: {count} commit{'s' if count != 1 else ''}{where}", ""]

    if activity["languages"]:
        lines += [f"**Languages:** {' · '.join(activity['languages'][:5])}", ""]

    oss = activity["external_repos"]
    if oss["total"]:
        named = ", ".join(f"[{r['name']}]({r['url']})" for r in oss["top"])
        plural = "repositories" if oss["total"] != 1 else "repository"
        suffix = f", including {named}" if named else ""
        lines += [
            f"<sub>All time: contributions to **{oss['total']}** {plural} "
            f"I don't own{suffix}.</sub>",
            "",
        ]

    lines.append(f"<sub>Updated automatically, last run {until:%b %-d, %Y}</sub>")
    return "\n".join(lines)


def splice(text: str, markers: tuple[str, str], replacement: str) -> str:
    start, end = markers
    i, j = text.find(start), text.find(end)
    if i == -1 or j == -1:
        raise SystemExit(f"Markers {start} / {end} not found in README")
    return text[: i + len(start)] + replacement + text[j:]


def extract(text: str, markers: tuple[str, str]) -> str:
    start, end = markers
    i, j = text.find(start), text.find(end)
    if i == -1 or j == -1:
        raise SystemExit(f"Markers {start} / {end} not found in README")
    return text[i + len(start) : j].strip()


# ---------------------------------------------------------------------------- main

def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    login = os.environ.get("GITHUB_USER")
    if not token or not login:
        raise SystemExit("GITHUB_TOKEN and GITHUB_USER are required")

    readme_path = Path(os.environ.get("README_PATH") or Path(__file__).resolve().parents[1] / "README.md")
    readme = readme_path.read_text()

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=7)
    activity = build_activity(fetch_contributions(login, token, since, until), login, token, since)

    # A quiet or fully-private week shouldn't leave a one-line section -- widen to a
    # month and relabel the heading so the dates still tell the truth.
    widened = False
    if is_thin(activity):
        since = until - timedelta(days=30)
        activity = build_activity(fetch_contributions(login, token, since, until), login, token, since)
        widened = True

    current_sentence = extract(readme, CURRENT_MARKERS)
    result = None
    if not is_empty(activity):
        try:
            result = ask_claude(
                redact(activity), format_window(since, until), current_sentence, load_projects()
            )
        except Exception as exc:  # narrative is a nice-to-have; the list is the point
            print(f"Narrative generation failed ({exc}); writing the plain list", file=sys.stderr)

    block = render_block(activity, (result or {}).get("narrative"), since, until, widened)
    readme = splice(readme, ACTIVITY_MARKERS, "\n" + block + "\n")

    if result and result.get("current_paragraph"):
        readme = splice(readme, CURRENT_MARKERS, result["current_paragraph"])

    readme_path.write_text(readme)
    print(f"Updated {readme_path} for {format_window(since, until)}")


if __name__ == "__main__":
    main()
