#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
生成 GitCode PR 检视报表（HTML），支持：
- 多仓库、多用户、多 PR 状态
- 页面筛选：只看未解决检视意见 / 隐藏没有未解决检视意见的 PR
  （CLI 参数 --only-unresolved / --hide-clean-prs 只影响页面默认勾选状态）
- 支持配置用户组（[[groups]]），前端可按组/用户筛选
- 输出一个静态 HTML，可直接部署到 GitHub Pages
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    import tomllib  # Python 3.11+
except ImportError:
    print("需要 Python 3.11+，因为脚本使用 tomllib 读取 TOML 配置文件", file=sys.stderr)
    sys.exit(1)


BASE_URL = "https://api.gitcode.com/api/v5"
CODE_STAT_SUFFIXES = {".cj", ".c", ".cpp", ".h", ".md"}


# ----------------- 数据结构 -----------------


@dataclass
class RepoConfig:
    owner: str
    repo: str
    states: List[str]
    per_page: int


@dataclass
class Config:
    access_token: Optional[str]
    users: List[str]
    groups: Dict[str, List[str]]
    repos: List[RepoConfig]
    code_stats: bool = True


@dataclass
class IssueInfo:
    number: str
    title: str
    state: str
    url: str
    labels: List[str] = field(default_factory=list)


@dataclass
class ReviewComment:
    id: int
    user: str
    body: str
    created_at: str
    updated_at: str
    resolved: Optional[bool] = None
    path: Optional[str] = None
    position: Optional[int] = None
    is_reply: bool = False
    parent_user: Optional[str] = None
    parent_id: Optional[int] = None


@dataclass
class PRInfo:
    number: int
    title: str
    state: str
    html_url: str
    created_at: str
    updated_at: str
    merged_at: Optional[str]
    source_branch: str = ""
    target_branch: str = ""
    issues: List[IssueInfo] = field(default_factory=list)
    comments: List[ReviewComment] = field(default_factory=list)
    additions: Optional[int] = None
    deletions: Optional[int] = None
    changed_files: Optional[int] = None
    file_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)


# ----------------- 配置读取 -----------------


def _normalize_states(obj: Dict[str, Any], default_states: List[str]) -> List[str]:
    """
    支持两种写法：
      state = "open"
      states = ["open", "merged"]
    最终统一成 List[str]。
    """
    if "states" in obj:
        v = obj["states"]
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(s) for s in v if s]
    if "state" in obj:
        v = obj["state"]
        if isinstance(v, str) and v:
            return [v]
    return list(default_states)


def load_config(path: str) -> Config:
    def _normalize_user_list(obj: Any) -> List[str]:
        if not obj:
            return []
        if isinstance(obj, list):
            return [str(u) for u in obj if u]
        if isinstance(obj, str):
            return [obj]
        return []

    with open(path, "rb") as f:
        data = tomllib.load(f)

    access_token = (
        data.get("access_token")
        or os.getenv("GITCODE_TOKEN")
        or os.getenv("GITCODE_PAT")
    )

    users = data.get("users")
    users_list = _normalize_user_list(users)

    groups_raw = data.get("groups") or []
    groups: Dict[str, List[str]] = {}
    if groups_raw:
        if not isinstance(groups_raw, list):
            raise ValueError(
                'groups 需要是数组表，例如 [[groups]] name="team" users=["alice"]'
            )
        for g in groups_raw:
            if not isinstance(g, dict):
                continue
            name = g.get("name")
            members = _normalize_user_list(g.get("users") or g.get("members"))
            if not name:
                raise ValueError("每个 [[groups]] 需要 name 字段")
            groups[name] = members

    if not users_list and not groups:
        raise ValueError(
            '配置文件必须包含 users 或 groups，例如 users=["alice"] 或 [[groups]]...'
        )

    global_states = data.get("states", ["open"])

    global_per_page = int(data.get("per_page", 30))
    if global_per_page < 1 or global_per_page > 100:
        global_per_page = 30

    code_stats_raw = data.get("code_stats", True)
    code_stats = bool(code_stats_raw) if isinstance(code_stats_raw, bool) else True

    repos_raw = data.get("repos")
    if not repos_raw or not isinstance(repos_raw, list):
        raise ValueError(
            "配置文件必须包含 [[repos]] 数组表，例如:\n"
            '[[repos]]\nowner = "org"\nrepo = "project"\n'
        )

    repos: List[RepoConfig] = []
    for r in repos_raw:
        owner = r.get("owner")
        repo = r.get("repo")
        if not owner or not repo:
            raise ValueError("[[repos]] 每一项必须包含 owner 和 repo 字段")

        states = _normalize_states(r, global_states)
        per_page = int(r.get("per_page", global_per_page))
        if per_page < 1 or per_page > 100:
            per_page = global_per_page

        repos.append(
            RepoConfig(owner=owner, repo=repo, states=states, per_page=per_page)
        )

    # 汇总用户列表：显式 users + groups 中的成员，去重保序
    seen_users: set[str] = set()
    merged_users: List[str] = []
    for name in users_list:
        if name not in seen_users:
            merged_users.append(name)
            seen_users.add(name)
    for members in groups.values():
        for name in members:
            if name not in seen_users:
                merged_users.append(name)
                seen_users.add(name)

    return Config(
        access_token=access_token,
        users=merged_users,
        groups=groups,
        repos=repos,
        code_stats=code_stats,
    )


# ----------------- HTTP 封装 -----------------


def gitcode_get(
    path: str, *, access_token: Optional[str], params: Dict[str, Any]
) -> Any:
    url = BASE_URL + path
    params = dict(params) if params else {}
    if access_token:
        params.setdefault("access_token", access_token)

    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitCode API 请求失败: {resp.status_code} {resp.text[:500]}"
        )
    return resp.json()


# ----------------- 拉取 PR / Issue / 评论 -----------------


def is_wip_title(title: str) -> bool:
    """
    粗略判断是否是 WIP PR：
    - 以 "WIP" / "[WIP]" 开头
    - 以 "wip" 开头（不区分大小写）
    """
    if not title:
        return False
    t = title.strip().lower()
    # 最常见几种格式
    if t.startswith("wip") or t.startswith("[wip]") or t.startswith("wip:"):
        return True
    return False


def fetch_prs_for_user(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    username: str,
) -> List[PRInfo]:
    all_prs: List[PRInfo] = []
    seen_numbers: set[int] = set()

    states = repo_cfg.states
    if "all" in states and len(states) > 1:
        states = ["all"]

    for state in states:
        page = 1
        while True:
            params = {
                "state": state,
                "author": username,
                "page": page,
                "per_page": repo_cfg.per_page,
                "only_count": "false",
            }

            data = gitcode_get(
                f"/repos/{repo_cfg.owner}/{repo_cfg.repo}/pulls",
                access_token=access_token,
                params=params,
            )

            if not isinstance(data, list) or not data:
                break

            for pr in data:
                num = int(pr.get("number", 0))

                # 🔴 1) 优先过滤 WIP
                title = pr.get("title", "") or ""
                # 有些 GitLab/GitCode 风格的接口还会给 work_in_progress/draft 字段
                if pr.get("work_in_progress") is True or pr.get("draft") is True:
                    continue

                if is_wip_title(title):
                    continue

                # 🔴 2) 去重
                if num in seen_numbers:
                    continue
                seen_numbers.add(num)

                head = pr.get("head") or {}
                base = pr.get("base") or {}

                all_prs.append(
                    PRInfo(
                        number=num,
                        title=title,
                        state=pr.get("state", ""),
                        html_url=pr.get("html_url", ""),
                        created_at=pr.get("created_at", ""),
                        updated_at=pr.get("updated_at", ""),
                        merged_at=pr.get("merged_at"),
                        source_branch=head.get("ref", "") or head.get("name", "") or "",
                        target_branch=base.get("ref", "") or base.get("name", "") or "",
                    )
                )

            if len(data) < repo_cfg.per_page:
                break

            page += 1

    return all_prs


def fetch_issues_for_pr(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    pr_number: int,
) -> List[IssueInfo]:
    """
    GET /repos/:owner/:repo/pulls/:number/issues  （若接口不存在则返回空列表）
    """
    try:
        data = gitcode_get(
            f"/repos/{repo_cfg.owner}/{repo_cfg.repo}/pulls/{pr_number}/issues",
            access_token=access_token,
            params={"page": 1, "per_page": 100},
        )
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    issues: List[IssueInfo] = []
    for it in data:
        labels = [lab.get("name", "") for lab in it.get("labels", [])]
        issues.append(
            IssueInfo(
                number=str(it.get("number", "")),
                title=it.get("title", ""),
                state=it.get("state", ""),
                url=it.get("url", "")
                .replace("api.gitcode", "gitcode")
                .replace("api/v5/repos/", ""),
                labels=labels,
            )
        )
    return issues


def fetch_files_for_pr(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    pr_number: int,
) -> tuple[Optional[int], Optional[int], Optional[int], Dict[str, Dict[str, int]]]:
    """
    GET /repos/:owner/:repo/pulls/:number/files
    返回 (additions, deletions, changed_files)，若失败返回 (None, None, None)
    """
    total_add = 0
    total_del = 0
    total_files = 0
    stats: Dict[str, Dict[str, int]] = {}
    seen_items: set[str] = set()
    page = 1
    per_page = 100
    max_pages = 10

    while True:
        try:
            data = gitcode_get(
                f"/repos/{repo_cfg.owner}/{repo_cfg.repo}/pulls/{pr_number}/files",
                access_token=access_token,
                params={"page": page, "per_page": per_page},
            )
        except Exception:
            return None, None, None, {}

        if not data:
            break

        items: List[Dict[str, Any]]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = [data]
        else:
            break

        new_items = 0
        for it in items:
            try:
                add = int(it.get("additions", 0) or 0)
            except (TypeError, ValueError):
                add = 0
            try:
                dele = int(it.get("deletions", 0) or 0)
            except (TypeError, ValueError):
                dele = 0
            name = it.get("filename") or it.get("new_path") or it.get("old_path") or ""
            key = f"{name}|{add}|{dele}"
            if key in seen_items:
                continue
            seen_items.add(key)
            new_items += 1
            ext = _ext_from_filename(name)
            if ext not in CODE_STAT_SUFFIXES:
                continue
            total_add += add
            total_del += dele
            total_files += 1
            bucket = stats.setdefault(ext, {"additions": 0, "deletions": 0, "files": 0})
            bucket["additions"] += add
            bucket["deletions"] += dele
            bucket["files"] += 1

        if not isinstance(data, list) or len(items) < per_page or new_items == 0:
            break

        page += 1
        if page > max_pages:
            break
        time.sleep(0.05)

    return total_add, total_del, total_files, stats


def _ext_from_filename(name: str) -> str:
    base = os.path.basename(name or "")
    if not base:
        return "(no_ext)"
    root, ext = os.path.splitext(base)
    if ext:
        return ext.lower()
    if base.startswith(".") and len(base) > 1 and root:
        return base.lower()
    return "(no_ext)"


def _infer_resolved(comment: Dict[str, Any]) -> Optional[bool]:
    if "resolved" in comment:
        val = comment.get("resolved")
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            v = val.lower()
            if v in ("true", "1", "yes", "resolved"):
                return True
            if v in ("false", "0", "no", "unresolved"):
                return False

    status = comment.get("status")
    if isinstance(status, str):
        v = status.lower()
        if v in ("resolved", "done"):
            return True
        if v in ("unresolved", "open", "todo"):
            return False

    return None


def fetch_repo_user_data(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    username: str,
    *,
    code_stats_enabled: bool = True,
) -> List[PRInfo]:
    """
    拉取一个仓库 + 一个用户的所有 PR，并填充 issues/comments，
    不在拉取阶段做过滤，交给前端页面自行过滤。
    """
    repo_name = f"{repo_cfg.owner}/{repo_cfg.repo}"
    t0 = time.perf_counter()
    print(f"[info] fetch start: {repo_name} {username}")
    prs = fetch_prs_for_user(access_token, repo_cfg, username)
    print(f"[info] fetch prs: {repo_name} {username} count={len(prs)}")

    result: List[PRInfo] = []
    total_comments = 0
    total_issues = 0
    total_files = 0
    total_add = 0
    total_del = 0
    for pr in prs:
        # 先拉评论
        comments = fetch_comments_for_pr(access_token, repo_cfg, pr.number)
        pr.comments = comments
        total_comments += len(comments)

        # 再拉 issues
        pr.issues = fetch_issues_for_pr(access_token, repo_cfg, pr.number)
        total_issues += len(pr.issues)

        # 再拉文件变更统计
        if code_stats_enabled:
            add, dele, files, stats = fetch_files_for_pr(
                access_token, repo_cfg, pr.number
            )
            pr.additions = add
            pr.deletions = dele
            pr.changed_files = files
            pr.file_stats = stats or {}
            if add is not None:
                total_add += add
            if dele is not None:
                total_del += dele
            if files is not None:
                total_files += files
            print(
                f"[info] pr detail: {repo_name} {username} "
                f"#{pr.number} comments={len(comments)} issues={len(pr.issues)} "
                f"files={files if files is not None else 'n/a'} "
                f"add={add if add is not None else 'n/a'} "
                f"del={dele if dele is not None else 'n/a'}"
            )
        else:
            print(
                f"[info] pr detail: {repo_name} {username} "
                f"#{pr.number} comments={len(comments)} issues={len(pr.issues)} "
                "files=skip"
            )

        result.append(pr)

    elapsed = time.perf_counter() - t0
    print(
        f"[info] fetch done: {repo_name} {username} prs={len(prs)} "
        f"comments={total_comments} issues={total_issues} files={total_files} "
        f"add={total_add} del={total_del} elapsed={elapsed:.1f}s"
    )
    return result


def fetch_comments_for_pr(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    pr_number: int,
) -> List[ReviewComment]:
    """
    GET /repos/:owner/:repo/pulls/:number/comments
    """
    comments: List[ReviewComment] = []
    page = 1

    while True:
        data = gitcode_get(
            f"/repos/{repo_cfg.owner}/{repo_cfg.repo}/pulls/{pr_number}/comments",
            access_token=access_token,
            params={"page": page, "per_page": 100, "comment_type": "diff_comment"},
        )

        if not isinstance(data, list) or not data:
            break

        def _make_comment(
            obj: Dict[str, Any],
            *,
            fallback_path=None,
            fallback_pos=None,
            is_reply: bool = False,
            parent_user: Optional[str] = None,
            parent_id: Optional[int] = None,
        ) -> ReviewComment:
            user_obj = obj.get("user") or {}
            login = (
                user_obj.get("login")
                or user_obj.get("username")
                or user_obj.get("name")
                or ""
            )
            pos = obj.get("position")
            if pos is None:
                diff_pos = obj.get("diff_position") or {}
                pos = diff_pos.get("start_new_line") or diff_pos.get("end_new_line")
            resolved_val = _infer_resolved(obj)
            if resolved_val is None:
                resolved_val = False
            return ReviewComment(
                id=int(obj.get("id", 0)),
                user=login,
                body=obj.get("body", ""),
                created_at=obj.get("created_at", ""),
                updated_at=obj.get("updated_at", ""),
                resolved=resolved_val,
                path=obj.get("path") or fallback_path,
                position=pos if pos is not None else fallback_pos,
                is_reply=is_reply,
                parent_user=parent_user,
                parent_id=parent_id,
            )

        for c in data:
            parent = _make_comment(c)
            comments.append(parent)

            replies = c.get("reply") or []
            if isinstance(replies, list) and replies:
                for r in replies:
                    comments.append(
                        _make_comment(
                            r,
                            fallback_path=parent.path,
                            fallback_pos=parent.position,
                            is_reply=True,
                            parent_user=parent.user,
                            parent_id=parent.id,
                        )
                    )

        if len(data) < 100:
            break

        page += 1
        time.sleep(0.05)

    return comments


# ----------------- HTML 生成 -----------------


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_comment_body(body: str) -> str:
    """
    极简 Markdown 渲染：
    - 支持 ```fenced code``` 代码块
    - 支持 `inline code`
    - 其余文本按行加 <br/>
    """
    if not body:
        return ""

    lines = body.splitlines()
    in_code = False
    code_lines: List[str] = []
    parts: List[str] = []

    def render_text_line(line: str) -> str:
        # 处理 `inline code`
        segments = line.split("`")
        out: List[str] = []
        for i, seg in enumerate(segments):
            if i % 2 == 0:
                out.append(escape_html(seg))
            else:
                out.append(
                    f"<code class='review-code-inline'>{escape_html(seg)}</code>"
                )
        return "".join(out)

    for line in lines:
        if line.startswith("```"):
            # fence 开关
            if not in_code:
                # 开始代码块
                in_code = True
                code_lines = []
            else:
                # 结束代码块
                code_html = (
                    "<pre class='review-code-block'><code>"
                    + escape_html("\n".join(code_lines))
                    + "</code></pre>"
                )
                parts.append(code_html)
                in_code = False
                code_lines = []
            continue

        if in_code:
            code_lines.append(line)
        else:
            parts.append(render_text_line(line) + "<br/>")

    # 如果 fence 没闭合，当普通文本处理
    if in_code and code_lines:
        for l in code_lines:
            parts.append(render_text_line(l) + "<br/>")

    return "".join(parts)


def build_html(
    cfg: Config,
    data: Dict[str, Dict[str, List[PRInfo]]],
    *,
    default_only_unresolved: bool,
    default_hide_clean_prs: bool,
    executed_at: str,
) -> str:
    """
    data 结构：
      { "owner/repo": { "username": [PRInfo, ...], ... }, ... }
    """
    title = "GitCode PR Review Report"
    allowed_pr_types = {
        "feat",
        "fix",
        "docs",
        "chore",
        "refactor",
        "test",
        "style",
        "perf",
        "ci",
    }

    def _parse_ts(ts: str) -> float:
        if not ts:
            return 0.0
        t = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(t).timestamp()
        except Exception:
            try:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                return 0.0

    def _pr_sort_key(pr: PRInfo) -> tuple:
        state_rank = {"open": 0, "merged": 1}
        rank = state_rank.get((pr.state or "").lower(), 2)
        # 越新的越靠前
        created_ts = -_parse_ts(pr.created_at)
        return (rank, created_ts, -pr.number)

    def _infer_pr_type(title: str) -> str:
        if not title:
            return ""
        t = title.strip()
        import re

        m = re.match(r"^([A-Za-z0-9_-]+)\s*:", t)
        if m:
            prefix = m.group(1).lower()
            return prefix if prefix in allowed_pr_types else ""
        return ""

    # 汇总 Issue 标签 / PR 类型，用于前端过滤
    seen_issue_labels: set[str] = set()
    issue_labels: List[str] = []
    seen_pr_types: set[str] = set()
    pr_types: List[str] = []
    seen_targets: set[str] = set()
    target_branches: List[str] = []
    for repo_prs in data.values():
        for prs in repo_prs.values():
            for pr in prs:
                pr_type = _infer_pr_type(pr.title or "")
                if pr_type and pr_type not in seen_pr_types:
                    pr_types.append(pr_type)
                    seen_pr_types.add(pr_type)
                tgt = (pr.target_branch or "").strip()
                if tgt and tgt not in seen_targets:
                    target_branches.append(tgt)
                    seen_targets.add(tgt)
                for iss in pr.issues:
                    for lab in iss.labels:
                        if not lab:
                            continue
                        lab_str = str(lab)
                        if lab_str not in seen_issue_labels:
                            issue_labels.append(lab_str)
                            seen_issue_labels.add(lab_str)

    style = """
    :root {
      --bg: #0f172a;
      --fg: #e5e7eb;
      --muted: #9ca3af;
      --muted2: #6b7280;
      --border: #1f2937;
      --surface-0: #0b1220;
      --surface-1: #0a101e;
      --surface-2: #020617;
      --card: #111827;
      --chip-bg: #1f2937;
      --chip-bg-2: #0b1220;
      --link: #93c5fd;
      --link-hover: #bfdbfe;
      --accent: #60a5fa;
      --shadow: rgba(0,0,0,0.35);
      --table-hover: rgba(148, 163, 184, 0.08);
      --pill-unresolved-bg: rgba(245, 158, 11, 0.12);
      --pill-unresolved-border: #f59e0b;
      --pill-unresolved-fg: #ffedd5;
      --pill-resolved-bg: rgba(34, 197, 94, 0.12);
      --pill-resolved-border: #22c55e;
      --pill-resolved-fg: #bbf7d0;
    }
    html[data-theme="light"] {
      --bg: #f8fafc;
      --fg: #0f172a;
      --muted: #475569;
      --muted2: #64748b;
      --border: #e2e8f0;
      --surface-0: #ffffff;
      --surface-1: #f1f5f9;
      --surface-2: #ffffff;
      --card: #ffffff;
      --chip-bg: #ffffff;
      --chip-bg-2: #f1f5f9;
      --link: #2563eb;
      --link-hover: #1d4ed8;
      --accent: #2563eb;
      --shadow: rgba(15, 23, 42, 0.12);
      --table-hover: rgba(15, 23, 42, 0.04);
      --pill-unresolved-bg: rgba(245, 158, 11, 0.12);
      --pill-unresolved-border: #d97706;
      --pill-unresolved-fg: #92400e;
      --pill-resolved-bg: rgba(34, 197, 94, 0.10);
      --pill-resolved-border: #16a34a;
      --pill-resolved-fg: #166534;
    }
    html[data-theme="dim"] {
      --bg: #0b1220;
      --fg: #e5e7eb;
      --muted: #9aa6b2;
      --muted2: #7b8794;
      --border: #223044;
      --surface-0: #0f172a;
      --surface-1: #0a101e;
      --surface-2: #0a1222;
      --card: #101a2b;
      --chip-bg: #17233a;
      --chip-bg-2: #0f172a;
      --link: #7dd3fc;
      --link-hover: #bae6fd;
      --accent: #38bdf8;
      --shadow: rgba(0,0,0,0.35);
      --table-hover: rgba(148, 163, 184, 0.10);
      --pill-unresolved-bg: rgba(245, 158, 11, 0.14);
      --pill-unresolved-border: #f59e0b;
      --pill-unresolved-fg: #ffedd5;
      --pill-resolved-bg: rgba(34, 197, 94, 0.14);
      --pill-resolved-border: #22c55e;
      --pill-resolved-fg: #bbf7d0;
    }
    html[data-theme="contrast"] {
      --bg: #000000;
      --fg: #ffffff;
      --muted: #e5e7eb;
      --muted2: #cbd5e1;
      --border: #ffffff;
      --surface-0: #000000;
      --surface-1: #0b0b0b;
      --surface-2: #000000;
      --card: #000000;
      --chip-bg: #0b0b0b;
      --chip-bg-2: #000000;
      --link: #fbbf24;
      --link-hover: #fde68a;
      --accent: #fbbf24;
      --shadow: rgba(0,0,0,0);
      --table-hover: rgba(255, 255, 255, 0.12);
      --pill-unresolved-bg: rgba(251, 191, 36, 0.12);
      --pill-unresolved-border: #fbbf24;
      --pill-unresolved-fg: #fbbf24;
      --pill-resolved-bg: rgba(34, 197, 94, 0.12);
      --pill-resolved-border: #22c55e;
      --pill-resolved-fg: #22c55e;
    }
    * { box-sizing: border-box; }
    a { color: var(--link); }
    a:hover { color: var(--link-hover); }
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 0;
      background: var(--bg);
      color: var(--fg);
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }
    h1 {
      font-size: 28px;
      margin-bottom: 8px;
    }
    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .page-header h1 {
      margin: 0;
    }
    .header-menu-btn {
      display: none;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--chip-bg-2);
      color: var(--fg);
      cursor: pointer;
      font-size: 16px;
    }
    @media (max-width: 720px) {
      .header-right .mini-stats,
      .header-right .refresh-stamp,
      .header-right #filter-toggle {
        display: none;
      }
      .header-menu-btn {
        display: inline-flex;
      }
    }
    .sub-title {
      font-size: 14px;
      color: var(--muted);
      margin-bottom: 24px;
    }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      margin-right: 6px;
      white-space: nowrap;  /* 🔴 不允许换行 */
      flex-shrink: 0;       /* 🔴 不要被压扁挤成多行 */
    }
    .badge-danger {
      background: #b91c1c;
      color: #fee2e2;
    }
    .badge-warn {
      background: #92400e;
      color: #ffedd5;
    }
    .badge-ok {
      background: #065f46;
      color: #d1fae5;
    }

    .repo-block {
      margin-top: 16px;
      margin-bottom: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface-2);
    }
    .repo-block > summary {
      list-style: none;
      cursor: pointer;
      padding: 10px 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .repo-block > summary::-webkit-details-marker {
      display: none;
    }
    .repo-title {
      font-size: 16px;
      font-weight: 600;
    }
    .repo-meta {
      font-size: 12px;
      color: var(--muted);
      margin-left: 8px;
    }
    .repo-chevron {
      font-size: 12px;
      color: var(--muted2);
      transition: transform 0.15s ease-out;
    }
    .repo-block[open] .repo-chevron {
      transform: rotate(90deg);
    }

    .repo-content {
      padding: 0 12px 10px 12px;
      border-top: 1px solid var(--border);
    }

    .user-block {
      margin-top: 8px;
      margin-bottom: 10px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-2);
    }
    .user-block > summary {
      list-style: none;
      cursor: pointer;
      padding: 8px 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .user-block > summary::-webkit-details-marker {
      display: none;
    }
    .user-title {
      font-size: 14px;
    }
    .user-meta {
      font-size: 11px;
      color: var(--muted);
      margin-left: 8px;
    }
    .user-chevron {
      font-size: 11px;
      color: var(--muted2);
      transition: transform 0.15s ease-out;
    }
    .user-block[open] .user-chevron {
      transform: rotate(90deg);
    }

    .user-content {
      padding: 6px 10px 8px 10px;
    }

    .pr-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 12px;
    }
    .pr-card {
      background: var(--card);
      border-radius: 12px;
      padding: 12px 14px;
      border: 1px solid var(--border);
      box-shadow: 0 10px 25px var(--shadow);
    }
    .pr-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
        min-width: 0;              /* 🔴 允许内部元素收缩 */
    }

    .pr-title {
        font-size: 14px;
        font-weight: 600;
        flex: 1;                    /* 🔴 占据剩余空间 */
        min-width: 0;               /* 🔴 允许被压缩 */
        overflow: hidden;           /* 🔴 超出用省略号 */
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .pr-meta {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .state-label {
      font-weight: 600;
    }
    .state-open {
      color: #22c55e;  /* 绿色 */
    }
    .state-merged {
      color: #a855f7;  /* 紫色 */
    }
    .state-other {
      color: var(--fg);  /* 默认 */
    }

    .pr-branch {
      font-size: 12px;
      color: var(--fg);
      margin-bottom: 4px;
    }
    .branch-target-pill {
      display: inline-block;
      padding: 0 6px;
      margin-left: 4px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      line-height: 1.6;
      background: var(--card);
      border: 1px solid var(--border);
    }
    .branch-target-main {
      background: rgba(34, 197, 94, 0.15);     /* 绿色主线 */
      border-color: #22c55e;
      color: #bbf7d0;
    }
    .branch-target-dev {
      background: rgba(59, 130, 246, 0.15);    /* 蓝色 dev */
      border-color: #3b82f6;
      color: #bfdbfe;
    }
    .branch-target-release {
      background: rgba(168, 85, 247, 0.18);    /* 紫色 release */
      border-color: #a855f7;
      color: #e9d5ff;
    }
    .branch-target-hotfix {
      background: rgba(239, 68, 68, 0.18);     /* 红色 hotfix */
      border-color: #ef4444;
      color: #fee2e2;
    }
    .branch-target-other {
      background: rgba(148, 163, 184, 0.15);   /* 灰色其他 */
      border-color: #64748b;
      color: #e5e7eb;
    }
    html[data-theme="light"] .branch-target-main {
      background: rgba(34, 197, 94, 0.10);
      border-color: #16a34a;
      color: #166534;
    }
    html[data-theme="light"] .branch-target-dev {
      background: rgba(59, 130, 246, 0.10);
      border-color: #2563eb;
      color: #1d4ed8;
    }
    html[data-theme="light"] .branch-target-release {
      background: rgba(168, 85, 247, 0.10);
      border-color: #7c3aed;
      color: #6d28d9;
    }
    html[data-theme="light"] .branch-target-hotfix {
      background: rgba(239, 68, 68, 0.10);
      border-color: #dc2626;
      color: #b91c1c;
    }
    html[data-theme="light"] .branch-target-other {
      background: rgba(100, 116, 139, 0.10);
      border-color: #64748b;
      color: #334155;
    }

    .pr-times {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .pr-code {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 4px;
    }
    .pr-link {
      font-size: 11px;
      color: var(--link);
      text-decoration: none;
    }
    .pr-link-inline, .issue-link {
      color: var(--link);
      text-decoration: none;
    }
    .pr-link-inline:hover, .issue-link:hover {
      text-decoration: underline;
    }
    .pr-link:hover {
      text-decoration: underline;
    }
    .section-title {
      font-size: 12px;
      font-weight: 600;
      margin-top: 6px;
      margin-bottom: 4px;
      color: var(--fg);
    }
    .issue-item, review-item {
      font-size: 11px;
      margin-bottom: 4px;
    }
        /* 每条 review 卡片 */
    .review-item {
      border-radius: 8px;
      padding: 8px 10px;
      margin-bottom: 8px;
      background: var(--surface-1);
      border: 1px solid var(--border);
      box-shadow: 0 2px 6px var(--shadow);
    }
    .review-item.unresolved {
      border-left: 4px solid #ef4444; /* 未解决：红色边 */
      background: rgba(239, 68, 68, 0.10);
    }
    .review-item.resolved {
      border-left: 4px solid #22c55e; /* 已解决：绿边 */
      background: rgba(34, 197, 94, 0.08);
    }
    .review-replies {
      margin-left: 14px;
      padding-left: 10px;
      border-left: 2px dashed var(--border);
    }
    .review-item.review-reply {
      background: var(--surface-0);
      border-style: dashed;
      border-color: var(--border);
      box-shadow: none;
    }
    .review-item.review-reply .review-header {
      font-size: 11px;
      color: var(--fg);
    }

    .review-header {
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 4px;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
    }
    .review-meta {
      font-size: 10px;
      color: var(--muted);
      margin-bottom: 4px;
    }

    /* 评论正文容器（短/长通用） */
    .review-body {
      font-size: 11px;
      line-height: 1.45;
    }

    /* 可折叠长评论 */
    .review-body-collapsible details {
      cursor: pointer;
    }
    .review-body-collapsible summary {
      list-style: none;
      font-size: 11px;
      color: var(--accent);
      padding: 2px 0;
    }
    .review-body-collapsible summary::-webkit-details-marker {
      display: none;
    }
    .review-body-collapsible summary::before {
      content: "▶";
      font-size: 9px;
      display: inline-block;
      margin-right: 4px;
      color: var(--muted2);
      transition: transform 0.15s ease-out;
    }
    .review-body-collapsible details[open] summary::before {
      transform: rotate(90deg);
    }
    .review-body-content {
      margin-top: 4px;
    }

    /* 内联代码 & 代码块 */
    .review-code-inline {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      background: var(--surface-0);
      padding: 0 3px;
      border-radius: 3px;
      border: 1px solid var(--border);
    }
    .review-code-block {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      background: var(--surface-2);
      border-radius: 6px;
      border: 1px solid var(--border);
      padding: 8px 10px;
      margin: 6px 0;
      font-size: 11px;
      overflow-x: auto;
      white-space: pre;
    }

    /* 按 reviewer 分组 */

    .reviewer-group {
      margin-top: 6px;
      margin-bottom: 8px;
      border-top: 1px dashed var(--border);
    }
    .reviewer-group > summary {
      list-style: none;
      cursor: pointer;
      padding-top: 4px;
      padding-bottom: 4px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .reviewer-group > summary::-webkit-details-marker {
      display: none;
    }
    .reviewer-group-title {
      font-size: 12px;
      color: var(--fg);
      display: flex;
      align-items: baseline;
    }
    .reviewer-group-title span {
      font-size: 11px;
      color: var(--muted);
      margin-left: 8px;
    }
    .reviewer-chevron {
      font-size: 10px;
      color: var(--muted2);
      margin-left: 8px;
      transition: transform 0.15s ease-out;
    }
    .reviewer-group[open] .reviewer-chevron {
      transform: rotate(90deg);
    }
    .reviewer-group-body {
      padding-left: 2px;
      padding-bottom: 4px;
    }

    .filter-container {
      margin: 6px 0 18px;
    }
    .filter-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
    }
    .header-right {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .mini-stats {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 4px 8px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-0);
      font-size: 12px;
      color: var(--muted);
    }
    .mini-stats b {
      color: var(--fg);
      font-weight: 600;
    }
    .refresh-stamp {
      font-size: 12px;
      color: var(--muted2);
      padding: 4px 6px;
      border-radius: 8px;
      border: 1px dashed var(--border);
      background: var(--surface-2);
    }
    .settings-modal {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 40;
    }
    .settings-modal[data-open="1"] {
      display: flex;
    }
    .settings-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(15, 23, 42, 0.45);
    }
    .settings-panel {
      position: relative;
      width: min(980px, 92vw);
      max-height: 86vh;
      display: flex;
      flex-direction: column;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: var(--surface-1);
      box-shadow: 0 24px 60px var(--shadow);
      overflow: hidden;
      z-index: 1;
    }
    .settings-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
    }
    .settings-title {
      font-size: 14px;
      font-weight: 600;
    }
    .settings-body {
      padding: 12px;
      overflow: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .filter-toggle {
      border: 1px solid var(--border);
      background: var(--chip-bg-2);
      color: var(--fg);
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
    }
    .filter-toggle:hover {
      border-color: var(--accent);
      color: var(--link-hover);
    }
    .filter-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface-0);
    }
    .fetch-state-box {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
      border-radius: 10px;
      border: 1px dashed var(--border);
      background: var(--surface-1);
    }
    .fetch-state-title {
      font-size: 12px;
      color: var(--muted);
    }
    .fetch-state-box .filter-label {
      margin: 0;
    }
    .top-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      padding: 8px 10px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface-0);
      margin-bottom: 8px;
    }
    .top-actions-left,
    .top-actions-right {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    @media (max-width: 900px) {
      .top-actions {
        flex-direction: column;
        align-items: stretch;
      }
      .top-actions-left,
      .top-actions-right {
        width: 100%;
        justify-content: flex-start;
      }
    }
    .token-box {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .token-input {
      min-width: 220px;
    }
    .token-status {
      font-size: 12px;
      color: var(--muted);
    }
    .token-status[data-state="error"] {
      color: #f59e0b;
    }
    .token-status[data-state="ok"] {
      color: #10b981;
    }
    .filter-chip-btn.secondary {
      background: var(--chip-bg-2);
    }
    .filter-select {
      background: var(--chip-bg-2);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
      height: 32px;
    }
    .config-panel {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface-1);
      padding: 4px 8px;
    }
    .config-panel > summary {
      cursor: pointer;
      font-size: 13px;
      list-style: none;
    }
    .config-panel > summary::-webkit-details-marker {
      display: none;
    }
    .config-body {
      padding: 8px 0 4px;
      display: grid;
      gap: 6px;
      min-width: 240px;
    }
    .config-label {
      font-size: 12px;
      color: var(--muted);
    }
    .config-textarea {
      background: var(--chip-bg-2);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 8px;
      font-size: 12px;
      resize: vertical;
      min-width: 260px;
    }
    .config-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .config-hint {
      font-size: 12px;
      color: var(--muted2);
    }
    .group-members {
      color: var(--muted2);
      margin-left: 4px;
      font-size: 12px;
    }
    .view-tabs {
      display: inline-flex;
      border: 1px solid var(--border);
      background: var(--surface-1);
      border-radius: 10px;
      overflow: hidden;
    }
    .review-controls {
      display: none;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .review-controls[data-show="1"] {
      display: inline-flex;
    }
    .filter-text {
      background: var(--chip-bg-2);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
      height: 32px;
      min-width: 180px;
    }
    .view-toggle-btn {
      border: 0;
      border-right: 1px solid var(--border);
      background: transparent;
      color: var(--fg);
      border-radius: 0;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
      height: 32px;
      display: inline-flex;
      align-items: center;
      white-space: nowrap;
    }
    .view-toggle-btn:last-child {
      border-right: 0;
    }
    .view-toggle-btn.active {
      background: var(--chip-bg);
      color: var(--link-hover);
    }
    .filter-summary {
      font-size: 12px;
      color: var(--muted);
      flex: 1;
    }

    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      padding: 14px;
      margin: 8px 0 22px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--surface-0);
    }
    .filter-group {
      flex: 1 1 260px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--surface-1);
    }
    .filter-group h3 {
      margin: 0 0 6px;
      font-size: 13px;
      color: var(--fg);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-group h3 span {
      font-size: 11px;
      color: var(--muted2);
      font-weight: 500;
    }
    .filter-label {
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
      margin: 4px 0;
    }
    .filter-bar input[type="checkbox"] {
      accent-color: var(--accent);
      width: 16px;
      height: 16px;
    }
    .filter-hint {
      font-size: 12px;
      color: var(--muted);
    }
    .filter-dates {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
    }
    .filter-dates input[type="date"] {
      background: var(--chip-bg-2);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 6px;
    }
    .date-picker-btn {
      border: 1px solid var(--border);
      background: var(--chip-bg-2);
      color: var(--fg);
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .date-picker-btn:hover {
      border-color: var(--accent);
      color: var(--link-hover);
    }
    .date-quick-btn {
      border: 1px solid var(--border);
      background: var(--chip-bg-2);
      color: var(--fg);
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .date-quick-btn:hover {
      border-color: var(--accent);
      color: var(--link-hover);
    }
    .filter-users {
      position: relative;
      display: inline-block;
    }
    .filter-user-toggle {
      border: 1px solid var(--border);
      background: var(--chip-bg-2);
      color: var(--fg);
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-user-toggle:hover {
      border-color: var(--accent);
      color: var(--link-hover);
    }
    .filter-user-panel {
      position: absolute;
      left: 0;
      top: calc(100% + 6px);
      min-width: 240px;
      background: var(--surface-0);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: 0 8px 24px var(--shadow);
      padding: 10px 12px;
      z-index: 50;
      display: none;
    }
    .filter-user-panel.open {
      display: block;
    }
    .filter-user-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 220px;
      overflow-y: auto;
      margin: 8px 0;
    }
    .filter-user-list::-webkit-scrollbar {
      width: 8px;
    }
    .filter-user-list::-webkit-scrollbar-track {
      background: var(--surface-1);
      border-radius: 8px;
    }
    .filter-user-list::-webkit-scrollbar-thumb {
      background: #334155;
      border-radius: 8px;
    }
    .filter-user-list::-webkit-scrollbar-thumb:hover {
      background: #475569;
    }
    .filter-user-list {
      scrollbar-width: thin;
      scrollbar-color: #334155 var(--surface-1);
    }
    .filter-user-item {
      font-size: 12px;
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .filter-user-actions {
      display: flex;
      gap: 8px;
    }
    .filter-chip-btn {
      border: 1px solid var(--border);
      background: var(--chip-bg);
      color: var(--fg);
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
      height: 32px;
      display: inline-flex;
      align-items: center;
    }
    .filter-chip-btn:hover {
      border-color: var(--accent);
      color: var(--link-hover);
    }
    .list-view {
      display: none;
      margin-top: 12px;
    }
    .issue-view {
      display: none;
      margin-top: 12px;
    }
    .received-view {
      display: none;
      margin-top: 12px;
    }
    .code-view {
      display: none;
      margin-top: 12px;
    }
    .issue-toggle {
      background: none;
      border: none;
      padding: 0;
      font: inherit;
      color: var(--link);
      cursor: pointer;
      text-align: left;
    }
    .issue-toggle:hover {
      text-decoration: underline;
    }
    .issue-detail {
      padding: 8px 10px;
    }
    .issue-detail-item {
      display: flex;
      gap: 8px;
      align-items: baseline;
      flex-wrap: wrap;
      margin: 6px 0;
    }
    .issue-detail-link {
      color: var(--link);
      text-decoration: none;
    }
    .issue-detail-link:hover {
      text-decoration: underline;
    }
    .issue-detail-meta {
      color: var(--muted);
      font-size: 11px;
    }
    .issue-pill {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      border: 1px solid var(--border);
      white-space: nowrap;
    }
    .issue-pill-unresolved {
      background: var(--pill-unresolved-bg);
      border-color: var(--pill-unresolved-border);
      color: var(--pill-unresolved-fg);
    }
    .issue-pill-resolved {
      background: var(--pill-resolved-bg);
      border-color: var(--pill-resolved-border);
      color: var(--pill-resolved-fg);
    }
    .list-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    .list-table th,
    .list-table td {
      border: 1px solid var(--border);
      padding: 8px;
      text-align: left;
    }
    .list-table th {
      background: var(--surface-0);
      color: var(--fg);
    }
    .list-table tr:nth-child(even) {
      background: var(--surface-1);
    }
    .list-table tr:hover {
      background: var(--table-hover);
    }
    .issue-detail-row td,
    .received-detail-row td,
    .code-detail-row td {
      background: var(--surface-1);
    }
    .stats-block {
      margin-top: 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--surface-1);
    }
    .stats-block h3 {
      margin: 0 0 8px;
      font-size: 13px;
      color: var(--fg);
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      font-size: 12px;
      color: var(--fg);
    }
    .stats-item {
      background: var(--surface-0);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
    }

    .empty-text {
      font-size: 12px;
      color: var(--muted2);
      margin-top: 4px;
    }
    .footer {
      margin-top: 40px;
      font-size: 11px;
      color: var(--muted2);
      text-align: center;
    }
    """

    group_json = json.dumps(cfg.groups, ensure_ascii=False)
    client_repos = [
        {
            "owner": r.owner,
            "repo": r.repo,
            "states": list(r.states),
            "per_page": r.per_page,
        }
        for r in cfg.repos
    ]
    client_config = {
        "baseUrl": BASE_URL,
        "repos": client_repos,
        "users": list(cfg.users),
        "groups": cfg.groups,
        "allowedPrTypes": sorted(allowed_pr_types),
        "codeStatSuffixes": sorted(CODE_STAT_SUFFIXES),
        "codeStatsEnabled": cfg.code_stats,
    }
    client_config_json = json.dumps(client_config, ensure_ascii=False)

    html_parts: List[str] = [
        "<!DOCTYPE html>",
        "<html lang='zh-CN'>",
        "<head>",
        "<meta charset='utf-8' />",
        f"<title>{escape_html(title)}</title>",
        "<meta name='viewport' content='width=device-width, initial-scale=1' />",
        "<style>",
        style,
        "</style>",
        "</head>",
        "<body>",
        "<div class='container'>",
    ]
    html_parts.append("<div class='page-header'>")
    html_parts.append(f"<h1>{escape_html(title)}</h1>")
    html_parts.append("<div class='header-right'>")
    html_parts.append(
        "<div class='mini-stats'>"
        "总 <b id='stat-total-mini'>0</b> · "
        "open <b id='stat-open-mini'>0</b> · "
        "merged <b id='stat-merged-mini'>0</b> · "
        "未解决 <b id='stat-unresolved-mini'>0</b>"
        "</div>"
    )
    html_parts.append(
        "<div class='refresh-stamp' id='refresh-stamp'>未刷新</div>"
    )
    html_parts.append(
        "<button type='button' class='filter-toggle' id='filter-toggle'>设置</button>"
    )
    html_parts.append(
        "<button type='button' class='header-menu-btn' id='header-menu-btn' title='打开设置'>☰</button>"
    )
    html_parts.append("</div>")
    html_parts.append("</div>")

    html_parts.append(
        f"<div class='sub-title'>执行时间：{escape_html(executed_at)}</div>"
    )
    filter_desc: List[str] = []
    if default_only_unresolved:
        filter_desc.append("默认仅展示未解决检视意见（可切换）")
    if default_hide_clean_prs:
        filter_desc.append("默认隐藏已关闭/已合并且无未解决检视意见的 PR（可切换）")
    filter_desc.append("状态、检视意见均可多选，支持创建日期筛选")
    filter_desc.append("当前筛选下无 PR 的用户默认隐藏，可切换显示")
    if cfg.groups:
        filter_desc.append("支持按用户组/个人筛选")
    if issue_labels:
        filter_desc.append("支持按 Issue 标签过滤")
    if pr_types:
        filter_desc.append("支持按 PR 类型前缀过滤（feat:/fix:/docs: 等）")
    if not filter_desc:
        filter_desc.append("可直接在页面上切换过滤，无需重新生成报表")
    html_parts.append(
        f"<div class='sub-title'>默认：{escape_html(' · '.join(filter_desc))}</div>"
    )

    html_parts.append("<div class='filter-container'>")
    html_parts.append("<div class='filter-header'>")
    html_parts.append(
        "<div class='filter-summary' id='filter-summary'>当前筛选：全部</div>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='top-actions'>")
    html_parts.append("<div class='top-actions-left'>")
    html_parts.append(
        "<div class='view-tabs'>"
        "<button type='button' class='view-toggle-btn active' id='view-card-btn' title='卡片视图'>卡片</button>"
        "<button type='button' class='view-toggle-btn' id='view-list-btn' title='列表视图'>列表</button>"
        "<button type='button' class='view-toggle-btn' id='view-issue-btn' title='检视意见（提出）'>提出</button>"
        "<button type='button' class='view-toggle-btn' id='view-received-btn' title='被提检视意见（收到）'>被提</button>"
        "<button type='button' class='view-toggle-btn' id='view-code-btn' title='代码量统计'>代码量</button>"
        "</div>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='top-actions-right'>")
    html_parts.append(
        "<select id='theme-select' class='filter-select' style='min-width:140px'>"
        "<option value='dark'>主题：暗色</option>"
        "<option value='dim'>主题：柔和</option>"
        "<option value='light'>主题：亮色</option>"
        "<option value='contrast'>主题：高对比</option>"
        "</select>"
    )
    html_parts.append(
        "<select id='preset-select' class='filter-select' style='min-width:160px'>"
        "<option value=''>预设：选择</option>"
        "</select>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='preset-apply'>应用预设</button>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='preset-save'>保存为预设</button>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='fetch-toggle'>抓取设置</button>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='refresh-data'>刷新数据</button>"
    )
    html_parts.append(
        "<span class='token-status' id='refresh-status'></span>"
    )
    html_parts.append("</div>")
    html_parts.append("</div>")
    html_parts.append("<div class='settings-modal' id='settings-modal' data-open='0'>")
    html_parts.append("<div class='settings-backdrop' id='settings-backdrop'></div>")
    html_parts.append("<div class='settings-panel'>")
    html_parts.append(
        "<div class='settings-header'>"
        "<div class='settings-title'>筛选设置</div>"
        "<button type='button' class='filter-toggle' id='settings-close'>关闭</button>"
        "</div>"
    )
    html_parts.append("<div class='settings-body'>")
    html_parts.append("<div class='filter-actions'>")
    html_parts.append(
        "<select id='sort-select' class='filter-select'>"
        "<option value='created' selected>排序：创建时间（新→旧）</option>"
        "<option value='updated'>排序：更新时间（新→旧）</option>"
        "<option value='unresolved'>排序：未解决意见数（多→少）</option>"
        "</select>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='quick-open-unresolved'>仅看 open 且有未解决意见</button>"
    )
    html_parts.append(
        "<div class='review-controls' id='review-controls' data-show='0'>"
        "<input type='text' id='review-user-keyword' class='filter-text' placeholder='筛选人名' />"
        "<select id='review-sort-select' class='filter-select'>"
        "<option value='total' selected>排序：总数</option>"
        "<option value='unresolved'>排序：未解决</option>"
        "<option value='resolved'>排序：已解决</option>"
        "<option value='name'>排序：姓名</option>"
        "</select>"
        "<button type='button' class='filter-chip-btn secondary' id='export-review-csv'>导出检视意见 CSV</button>"
        "</div>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='export-csv'>导出当前筛选 CSV</button>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='filter-bar' id='filter-bar' data-open='1'>")
    # 状态
    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>PR 状态 <span>(多选)</span></h3>")
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' class='filter-state-checkbox' value='open' checked />"
        " 状态：open"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' class='filter-state-checkbox' value='merged' checked />"
        " 状态：merged"
        "</label>"
    )
    html_parts.append("</div>")

    # 评论（拆分：PR 过滤 vs 展示控制）
    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>检视意见（PR 过滤） <span>(多选)</span></h3>")
    html_parts.append(
        "<div class='filter-hint'>下方选项决定哪些 PR 会保留在列表中。</div>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' class='filter-comment-checkbox' value='unresolved' checked />"
        " 未解决检视意见"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' class='filter-comment-checkbox' value='resolved' checked />"
        " 已解决检视意见"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' class='filter-comment-checkbox' value='none' checked />"
        " 无检视意见"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        f"<input type='checkbox' id='filter-hide-clean' {'checked' if default_hide_clean_prs else ''} />"
        " 隐藏没有未解决检视意见的已关闭/已合并 PR"
        "</label>"
    )
    html_parts.append("</div>")

    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>检视意见（评论显示）</h3>")
    html_parts.append(
        "<div class='filter-hint'>仅影响评论的显示/隐藏，不改变 PR 是否保留；是否保留 PR 由上方“检视意见（PR 过滤）”决定。</div>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        f"<input type='checkbox' id='filter-unresolved' {'checked' if default_only_unresolved else ''} />"
        " 仅显示未解决检视意见"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' id='filter-resolved-only' /> 仅显示已解决检视意见"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' id='filter-hide-replies' />"
        " 不展示回复（仅显示主评论）"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<span style='min-width:96px'>回复包含：</span>"
        "<input type='text' id='filter-comment-keyword' class='filter-text' placeholder='输入关键字，模糊匹配' />"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<span style='min-width:96px'>回复不包含：</span>"
        "<input type='text' id='filter-comment-exclude' class='filter-text' placeholder='输入关键字，排除匹配' />"
        "</label>"
    )
    html_parts.append("</div>")

    # Issue 标签
    issue_group_style = "" if issue_labels else " style='display:none'"
    html_parts.append(
        f"<div class='filter-group' id='filter-issue-group'{issue_group_style}>"
    )
    html_parts.append("<h3>Issue 标签 <span>(多选)</span></h3>")
    html_parts.append("<div class='filter-user-list' id='filter-issue-list'>")
    for lab in issue_labels:
        html_parts.append(
            "<label class='filter-label'>"
            f"<input type='checkbox' class='filter-issue-label-checkbox' value='{escape_html(lab)}' /> "
            f"{escape_html(lab)}"
            "</label>"
        )
    html_parts.append("</div>")
    html_parts.append("</div>")

    # PR 类型（标题前缀）
    pr_type_group_style = "" if pr_types else " style='display:none'"
    html_parts.append(
        f"<div class='filter-group' id='filter-pr-type-group'{pr_type_group_style}>"
    )
    html_parts.append("<h3>PR 类型 <span>(title 前缀，多选)</span></h3>")
    html_parts.append("<div class='filter-user-list' id='filter-pr-type-list'>")
    for t in pr_types:
        html_parts.append(
            "<label class='filter-label'>"
            f"<input type='checkbox' class='filter-pr-type-checkbox' value='{escape_html(t)}' /> "
            f"{escape_html(t)}"
            "</label>"
        )
    html_parts.append("</div>")
    html_parts.append("</div>")

    target_group_style = "" if target_branches else " style='display:none'"
    html_parts.append(
        f"<div class='filter-group' id='filter-target-group'{target_group_style}>"
    )
    html_parts.append("<h3>目标分支 <span>(多选)</span></h3>")
    html_parts.append("<div class='filter-user-list' id='filter-target-list'>")
    for t in target_branches:
        html_parts.append(
            "<label class='filter-label'>"
            f"<input type='checkbox' class='filter-target-checkbox' value='{escape_html(t)}' checked /> "
            f"{escape_html(t)}"
            "</label>"
        )
    html_parts.append("</div>")
    html_parts.append("</div>")

    # 时间 / 用户开关
    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>时间 / 用户</h3>")
    html_parts.append(
        "<div class='filter-dates'>"
        "<select id='filter-date-field' class='filter-select' style='margin-left:8px'>"
        "<option value='created' selected>按创建时间</option>"
        "<option value='updated'>按更新时间</option>"
        "</select>"
        "<input type='date' id='filter-date-start' />"
        "<button type='button' class='date-picker-btn' data-picker='start'>选择</button>"
        "<span>至</span>"
        "<input type='date' id='filter-date-end' />"
        "<button type='button' class='date-picker-btn' data-picker='end'>选择</button>"
        "</div>"
    )
    html_parts.append(
        "<div class='filter-dates'>"
        "快捷："
        "<button type='button' class='date-quick-btn' data-range='7'>近 7 天</button>"
        "<button type='button' class='date-quick-btn' data-range='30'>近 30 天</button>"
        "<button type='button' class='date-quick-btn' data-range='90'>近 90 天</button>"
        "<button type='button' class='date-quick-btn' data-range='0'>全部</button>"
        "</div>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' id='filter-hide-empty-users' checked />"
        " 隐藏当前筛选下没有 PR 的用户"
        "</label>"
    )
    html_parts.append("</div>")

    # 用户 / 组
    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>用户 / 组</h3>")
    # 用户筛选区域（默认全选），用下拉面板减少占位
    user_dropdown_style = "" if cfg.users else " style='display:none'"
    html_parts.append(
        f"<div class='filter-users' id='filter-user-dropdown'{user_dropdown_style}>"
    )
    html_parts.append(
        "<button type='button' class='filter-user-toggle' id='filter-user-toggle'>"
        "用户：全部"
        "</button>"
    )
    html_parts.append("<div class='filter-user-panel' id='filter-user-panel'>")
    html_parts.append("<div class='filter-user-actions'>")
    html_parts.append(
        "<button type='button' class='filter-chip-btn' id='filter-user-all'>全选</button>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn' id='filter-user-none'>全不选</button>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='filter-user-list' id='filter-user-list'>")
    if cfg.users:
        for uname in cfg.users:
            html_parts.append(
                "<label class='filter-user-item'>"
                f"<input type='checkbox' class='filter-user-checkbox' value='{escape_html(uname)}' checked /> "
                f"{escape_html(uname)}"
                "</label>"
            )
    else:
        html_parts.append("<div class='empty-text'>配置中没有用户</div>")
    html_parts.append("</div>")  # list
    html_parts.append("</div>")  # panel
    html_parts.append("</div>")  # dropdown

    # 用户组筛选
    group_dropdown_style = "" if cfg.groups else " style='display:none'"
    html_parts.append(
        f"<div class='filter-users' id='filter-group-dropdown'{group_dropdown_style}>"
    )
    html_parts.append(
        "<button type='button' class='filter-user-toggle' id='filter-group-toggle'>"
        "用户组：全部"
        "</button>"
    )
    html_parts.append("<div class='filter-user-panel' id='filter-group-panel'>")
    html_parts.append("<div class='filter-user-actions'>")
    html_parts.append(
        "<button type='button' class='filter-chip-btn' id='filter-group-all'>全选</button>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn' id='filter-group-none'>全不选</button>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='filter-user-list' id='filter-group-list'>")
    for gname, members in cfg.groups.items():
        members_text = ", ".join(escape_html(m) for m in members)
        html_parts.append(
            "<label class='filter-user-item'>"
            f"<input type='checkbox' class='filter-group-checkbox' value='{escape_html(gname)}' checked /> "
            f"{escape_html(gname)}"
            f" <span style='color:#9ca3af'>( {members_text} )</span>"
            "</label>"
        )
    html_parts.append("</div>")  # list
    html_parts.append("</div>")  # panel
    html_parts.append("</div>")  # dropdown
    html_parts.append("</div>")  # filter-group 用户/组
    html_parts.append("</div>")  # filter-bar
    html_parts.append("</div>")  # settings-body
    html_parts.append("</div>")  # settings-panel
    html_parts.append("</div>")  # settings-modal
    html_parts.append("<div class='settings-modal' id='fetch-modal' data-open='0'>")
    html_parts.append("<div class='settings-backdrop' id='fetch-backdrop'></div>")
    html_parts.append("<div class='settings-panel'>")
    html_parts.append(
        "<div class='settings-header'>"
        "<div class='settings-title'>抓取设置</div>"
        "<button type='button' class='filter-toggle' id='fetch-close'>关闭</button>"
        "</div>"
    )
    html_parts.append("<div class='settings-body'>")
    html_parts.append("<div class='filter-actions'>")
    html_parts.append(
        "<select id='fetch-mode-select' class='filter-select' style='min-width:220px'>"
        "<option value='none' selected>抓取范围：不限制</option>"
        "<option value='details'>抓取范围：仅拉取日期内详情</option>"
        "<option value='api'>抓取范围：API 过滤 + 日期内详情</option>"
        "</select>"
    )
    html_parts.append(
        "<div class='fetch-state-box'>"
        "<span class='fetch-state-title'>抓取状态</span>"
        "<label class='filter-label'>"
        "<input type='checkbox' class='fetch-state-checkbox' value='open' checked /> open"
        "</label>"
        "<label class='filter-label'>"
        "<input type='checkbox' class='fetch-state-checkbox' value='merged' checked /> merged"
        "</label>"
        "</div>"
    )
    html_parts.append("</div>")
    html_parts.append("<div class='filter-bar'>")
    html_parts.append("<div class='filter-group'>")
    html_parts.append("<h3>抓取日期范围</h3>")
    html_parts.append(
        "<div class='filter-hint'>仅影响刷新抓取，不改变页面筛选。</div>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<span style='min-width:96px'>抓取字段：</span>"
        "<select id='fetch-date-field' class='filter-select'>"
        "<option value='created' selected>创建时间</option>"
        "<option value='updated'>更新时间</option>"
        "</select>"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<span style='min-width:96px'>开始日期：</span>"
        "<input type='date' id='fetch-date-start' class='filter-text' style='min-width:150px' />"
        "<button type='button' class='date-picker-btn' data-picker='fetch-start'>选择</button>"
        "</label>"
    )
    html_parts.append(
        "<label class='filter-label'>"
        "<span style='min-width:96px'>结束日期：</span>"
        "<input type='date' id='fetch-date-end' class='filter-text' style='min-width:150px' />"
        "<button type='button' class='date-picker-btn' data-picker='fetch-end'>选择</button>"
        "</label>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='fetch-date-clear'>清空抓取日期</button>"
    )
    html_parts.append("</div>")
    html_parts.append("</div>")
    html_parts.append(
        "<details class='config-panel' id='client-config-panel'>"
        "<summary>用户/组设置</summary>"
        "<div class='config-body'>"
        "<label class='config-label'>用户（每行一个）</label>"
        "<textarea id='config-users' class='config-textarea' rows='4' placeholder='alice\\nbob'></textarea>"
        "<label class='config-label'>用户组（格式：组名: user1, user2）</label>"
        "<textarea id='config-groups' class='config-textarea' rows='4' placeholder='teamA: alice, bob'></textarea>"
        "<div class='config-actions'>"
        "<button type='button' class='filter-chip-btn secondary' id='config-apply'>应用并刷新</button>"
        "<button type='button' class='filter-chip-btn' id='config-reset'>恢复默认</button>"
        "</div>"
        "<div class='config-hint'>设置会保存在浏览器本地，仅影响当前页面。</div>"
        "</div>"
        "</details>"
    )
    html_parts.append(
        "<details class='config-panel' id='fetch-tuning-panel'>"
        "<summary>抓取性能设置</summary>"
        "<div class='config-body'>"
        "<label class='config-label'>请求间隔（毫秒，建议 >= 800）</label>"
        "<input type='number' id='config-request-interval' class='filter-text' "
        "min='500' step='100' placeholder='1200' style='min-width:140px' />"
        "<label class='config-label'>仓库并发（同时拉取用户/仓库）</label>"
        "<input type='number' id='config-repo-concurrency' class='filter-text' "
        "min='1' max='6' step='1' placeholder='4' style='min-width:140px' />"
        "<label class='config-label'>详情并发（单用户 PR 详情）</label>"
        "<input type='number' id='config-detail-concurrency' class='filter-text' "
        "min='1' max='10' step='1' placeholder='6' style='min-width:140px' />"
        "<div class='config-actions'>"
        "<button type='button' class='filter-chip-btn secondary' id='config-tuning-apply'>保存设置</button>"
        "<button type='button' class='filter-chip-btn' id='config-tuning-reset'>恢复默认</button>"
        "</div>"
        "<div class='config-hint'>仅影响后续刷新；并发越高、间隔越小越容易触发限流。</div>"
        "</div>"
        "</details>"
    )
    html_parts.append(
        "<div class='token-box'>"
        "<input type='password' id='api-token' class='filter-text token-input' "
        "placeholder='API Token（仅保存在本地浏览器）' />"
        "<button type='button' class='filter-chip-btn secondary' id='token-clear'>清除 Token</button>"
        "<span class='token-status' id='token-status'>未设置</span>"
        "</div>"
    )
    html_parts.append("</div>")  # settings-body
    html_parts.append("</div>")  # settings-panel
    html_parts.append("</div>")  # fetch-modal
    html_parts.append("</div>")  # filter-container

    # 统计概览（顶部已提供迷你统计）

    html_parts.append("<div id='card-view'>")
    if not data:
        html_parts.append("<p class='empty-text'>没有任何符合条件的 PR。</p>")
    else:
        for repo_name, users_prs in data.items():
            # 统计这个 repo 有多少 PR（过滤后）
            total_prs = sum(len(v) for v in users_prs.values())

            html_parts.append(f"<details class='repo-block' open data-repo-block>")
            html_parts.append("<summary>")
            html_parts.append(f"<div class='repo-title'>仓库：{escape_html(repo_name)}")
            html_parts.append(
                f"<span class='repo-meta' data-repo-count>共 {total_prs} 个 PR（页面可再筛选）</span>"
            )
            html_parts.append("</div>")
            html_parts.append("<div class='repo-chevron'>▶</div>")
            html_parts.append("</summary>")

            html_parts.append("<div class='repo-content'>")

            for username, prs in users_prs.items():
                sorted_prs = sorted(prs, key=_pr_sort_key)
                if len(prs) == 0:
                    continue
                html_parts.append(
                    f"<details class='user-block' open data-user-block data-username='{escape_html(username)}'>"
                )
                html_parts.append("<summary>")
                html_parts.append(
                    f"<div class='user-title'>用户：{escape_html(username)}"
                )
                html_parts.append(
                    f"<span class='user-meta' data-user-count>共 {len(prs)} 个 PR</span>"
                )
                html_parts.append("</div>")
                html_parts.append("<div class='user-chevron'>▶</div>")
                html_parts.append("</summary>")

                html_parts.append("<div class='user-content'>")

                if not prs:
                    pass
                    # html_parts.append(
                    #     "<div class='empty-text'>该用户在当前筛选条件下没有 PR。</div>"
                    # )
                else:
                    html_parts.append("<div class='pr-grid'>")
                    for pr in sorted_prs:
                        all_comments = pr.comments
                        parent_comments = [cm for cm in all_comments if not cm.is_reply]
                        unresolved_count = sum(
                            1 for cm in parent_comments if cm.resolved is False
                        )
                        resolved_count = sum(
                            1 for cm in parent_comments if cm.resolved is True
                        )

                        issue_labels_flat: List[str] = []
                        for iss in pr.issues:
                            for lab in iss.labels:
                                if not lab:
                                    continue
                                if lab not in issue_labels_flat:
                                    issue_labels_flat.append(lab)

                        pr_type = _infer_pr_type(pr.title or "")

                        code_known = (
                            pr.additions is not None
                            and pr.deletions is not None
                            and pr.changed_files is not None
                        )
                        ext_summary = ""
                        if pr.file_stats:
                            ext_list = sorted(
                                pr.file_stats.items(),
                                key=lambda kv: (
                                    kv[1].get("additions", 0)
                                    + kv[1].get("deletions", 0)
                                ),
                                reverse=True,
                            )
                            top = []
                            for ext, stat in ext_list[:3]:
                                top.append(
                                    f"{ext} +{stat.get('additions', 0)}/-{stat.get('deletions', 0)}"
                                )
                            if top:
                                ext_summary = " · 后缀：" + " · ".join(top)
                                if len(ext_list) > 3:
                                    ext_summary += " 等"
                        if code_known:
                            code_text = (
                                f"代码变更：+{pr.additions} / -{pr.deletions} · 文件 {pr.changed_files}{ext_summary}"
                            )
                        else:
                            code_text = "代码变更：未知"

                        if unresolved_count > 0:
                            badge_cls = "badge-danger"
                            badge_text = f"{unresolved_count} 未解决"
                        elif pr.comments:
                            badge_cls = "badge-ok"
                            badge_text = "无未解决检视意见"
                        else:
                            badge_cls = "badge-warn"
                            badge_text = "无检视意见"

                        state_lower = (pr.state or "").lower()
                        html_parts.append(
                            "<div class='pr-card'"
                            f" data-state='{escape_html(state_lower)}'"
                            f" data-has-unresolved='{1 if unresolved_count > 0 else 0}'"
                            f" data-total-comments='{len(all_comments)}'"
                            f" data-unresolved-count='{unresolved_count}'"
                            f" data-resolved-count='{resolved_count}'"
                            f" data-code-known='{'1' if code_known else '0'}'"
                            f" data-additions='{'' if pr.additions is None else pr.additions}'"
                            f" data-deletions='{'' if pr.deletions is None else pr.deletions}'"
                            f" data-changed-files='{'' if pr.changed_files is None else pr.changed_files}'"
                            f" data-code-stats='{escape_html(json.dumps(pr.file_stats, ensure_ascii=False))}'"
                            f" data-created='{escape_html(pr.created_at)}'"
                            f" data-updated='{escape_html(pr.updated_at)}'"
                            f" data-issue-labels='{escape_html('||'.join(issue_labels_flat))}'"
                            f" data-pr-number='{pr.number}'"
                            f" data-title='{escape_html(pr.title or '')}'"
                            f" data-url='{escape_html(pr.html_url or '')}'"
                            f" data-repo='{escape_html(repo_name)}'"
                            f" data-username='{escape_html(username)}'"
                            f" data-source='{escape_html(pr.source_branch)}'"
                            f" data-target='{escape_html(pr.target_branch)}'"
                            f" data-pr-type='{escape_html(pr_type)}'>"
                        )

                        html_parts.append("<div class='pr-header'>")

                        # PR 标题：如果有链接，整段标题变成可点击
                        title_text = f"#{pr.number} {pr.title or ''}"
                        if pr.html_url:
                            title_html = (
                                f"<a class='pr-link-inline' "
                                f"href='{escape_html(pr.html_url)}' "
                                f"target='_blank' rel='noopener noreferrer'>"
                                f"{escape_html(title_text)}</a>"
                            )
                        else:
                            title_html = escape_html(title_text)

                        html_parts.append(f"<div class='pr-title'>{title_html}</div>")

                        html_parts.append(
                            f"<span class='badge {badge_cls}'>{escape_html(badge_text)}</span>"
                        )
                        html_parts.append("</div>")  # pr-header

                        # 状态颜色：open 绿色，merged 紫色，其它默认
                        if state_lower == "open":
                            state_cls = "state-open"
                        elif state_lower == "merged":
                            state_cls = "state-merged"
                        else:
                            state_cls = "state-other"
                        html_parts.append(
                            "<div class='pr-meta'>状态："
                            f"<span class='state-label {state_cls}'>{escape_html(pr.state)}</span>"
                            "</div>"
                        )

                        # 分支行：source → target，并对 target 高亮
                        if pr.target_branch:
                            tb = pr.target_branch or ""
                            tb_lower = tb.lower()

                            if tb_lower in ("main", "master", "trunk"):
                                tgt_cls = "branch-target-main"
                            elif tb_lower in ("dev", "develop") or "dev" in tb_lower:
                                tgt_cls = "branch-target-dev"
                            elif tb_lower.startswith("release/") or tb_lower.startswith(
                                "release-"
                            ):
                                tgt_cls = "branch-target-release"
                            elif tb_lower.startswith("hotfix/") or tb_lower.startswith(
                                "hotfix-"
                            ):
                                tgt_cls = "branch-target-hotfix"
                            else:
                                tgt_cls = "branch-target-other"

                            src = pr.source_branch or ""
                            branch_html = (
                                f"{escape_html(src)} → "
                                f"<span class='branch-target-pill {tgt_cls}'>"
                                f"{escape_html(tb)}</span>"
                            )
                        else:
                            # 没有 target_branch 的情况，保持原来纯文本
                            branch_html = (
                                escape_html(pr.source_branch)
                                if pr.source_branch
                                else ""
                            )

                        if branch_html:
                            html_parts.append(
                                f"<div class='pr-branch'>分支：{branch_html}</div>"
                            )
                            times_line = f"创建：{escape_html(pr.created_at)}"
                            if pr.updated_at:
                                times_line += f" ｜ 更新：{escape_html(pr.updated_at)}"
                            html_parts.append(
                                f"<div class='pr-times'>{times_line}</div>"
                            )
                            html_parts.append(
                                f"<div class='pr-code'>{escape_html(code_text)}</div>"
                            )

                        # Issues
                        html_parts.append(
                            "<div class='section-title'>关联 Issues</div>"
                        )
                        if not pr.issues:
                            html_parts.append(
                                "<div class='empty-text'>无关联 Issue</div>"
                            )
                        else:
                            for iss in pr.issues:
                                labels_str = (
                                    f"（labels: {', '.join(iss.labels)}）"
                                    if iss.labels
                                    else ""
                                )

                                issue_text = f"#{iss.number} [{iss.state}] {iss.title}{labels_str}"

                                if iss.url:
                                    issue_html = (
                                        f"<a class='issue-link' "
                                        f"href='{escape_html(iss.url)}' "
                                        f"target='_blank' rel='noopener noreferrer'>"
                                        f"{escape_html(issue_text)}</a>"
                                    )
                                else:
                                    issue_html = escape_html(issue_text)

                                html_parts.append(
                                    f"<div class='issue-item'>{issue_html}</div>"
                                )

                        # Reviews
                        html_parts.append("<div class='section-title'>检视意见</div>")

                        html_parts.append("<div class='reviews' data-review-wrapper>")

                        if not all_comments:
                            html_parts.append(
                                "<div class='empty-text' data-empty-all>无需要 resolved 状态的检视意见</div>"
                            )
                        else:
                            # 1. 按 reviewer 分组（仅主评论，保留原有顺序）
                            from collections import OrderedDict

                            parent_comments_all = [
                                cm for cm in all_comments if not cm.is_reply
                            ]
                            grouped: "OrderedDict[str, List[ReviewComment]]" = (
                                OrderedDict()
                            )
                            for cm in parent_comments_all:
                                key = cm.user or "(unknown)"
                                if key not in grouped:
                                    grouped[key] = []
                                grouped[key].append(cm)

                            # 2. 回复索引（跨作者，挂到对应主评论）
                            replies_by_parent: Dict[int, List[ReviewComment]] = {}
                            orphan_replies: List[ReviewComment] = []
                            parent_ids = {cm.id for cm in parent_comments_all}
                            for cm in all_comments:
                                if not cm.is_reply:
                                    continue
                                if cm.parent_id is not None and cm.parent_id in parent_ids:
                                    replies_by_parent.setdefault(cm.parent_id, []).append(cm)
                                else:
                                    orphan_replies.append(cm)

                            # 3. 逐个 reviewer 输出
                            for reviewer, parent_comments in grouped.items():
                                parent_count = len(parent_comments)
                                parent_unresolved = sum(
                                    1 for cm in parent_comments if cm.resolved is False
                                )
                                parent_resolved = sum(
                                    1 for cm in parent_comments if cm.resolved is True
                                )
                                # 默认展开，想默认收起就把 open 去掉
                                html_parts.append(
                                    "<details class='reviewer-group' open>"
                                )
                                html_parts.append("<summary>")

                                html_parts.append(
                                    "<div class='reviewer-group-title'>"
                                    f"{escape_html(reviewer)}"
                                    f"<span>{parent_count} 条检视意见（未解决 {parent_unresolved} · 已解决 {parent_resolved}）</span>"
                                    "</div>"
                                )
                                html_parts.append(
                                    "<div class='reviewer-chevron'>▶</div>"
                                )

                                html_parts.append("</summary>")

                                html_parts.append("<div class='reviewer-group-body'>")

                                def render_comment(
                                    cm: ReviewComment, *, is_reply: bool = False
                                ):
                                    is_resolved = cm.resolved is True
                                    status_cls = (
                                        "reply"
                                        if is_reply
                                        else (
                                            "resolved" if is_resolved else "unresolved"
                                        )
                                    )
                                    status_text = (
                                        "回复"
                                        if is_reply
                                        else ("已解决" if is_resolved else "未解决")
                                    )
                                    resolved_attr = "true" if is_resolved else "false"
                                    is_reply_attr = "1" if is_reply else "0"
                                    user_attr = escape_html(cm.user or "")
                                    parent_user_attr = escape_html(cm.parent_user or "")
                                    parent_id_attr = (
                                        f" data-parent-id='{cm.parent_id}'"
                                        if is_reply and cm.parent_id is not None
                                        else ""
                                    )
                                    comment_id_attr = f" data-comment-id='{cm.id}'"

                                    loc = ""
                                    if cm.path:
                                        loc = cm.path
                                        if cm.position is not None:
                                            loc += f":{cm.position}"

                                    header_left = status_text
                                    if loc:
                                        header_left += f" · {loc}"

                                    html_parts.append(
                                        f"<div class='review-item {status_cls}{' review-reply' if is_reply else ''}' data-resolved='{resolved_attr}' data-is-reply='{is_reply_attr}' data-user='{user_attr}' data-parent-user='{parent_user_attr}'{parent_id_attr}{comment_id_attr}>"
                                    )

                                    # header
                                    html_parts.append(
                                        "<div class='review-header'>"
                                        f"<span>{escape_html(header_left)}</span>"
                                        "</div>"
                                    )

                                    # 时间
                                    html_parts.append(
                                        f"<div class='review-meta'>创建：{escape_html(cm.created_at)} ｜ 更新：{escape_html(cm.updated_at)}</div>"
                                    )

                                    # body（这里用你现在的 render_comment_body + 折叠逻辑）
                                    if cm.body:
                                        body_html = render_comment_body(cm.body)
                                        line_count = cm.body.count("\n") + 1
                                        is_long = line_count >= 8 or len(cm.body) >= 400

                                        if is_long:
                                            html_parts.append(
                                                "<div class='review-body review-body-collapsible'>"
                                                "<details>"
                                                f"<summary>展开完整评论（约 {line_count} 行）</summary>"
                                                f"<div class='review-body-content'>{body_html}</div>"
                                                "</details>"
                                                "</div>"
                                            )
                                        else:
                                            html_parts.append(
                                                f"<div class='review-body'>{body_html}</div>"
                                            )

                                    html_parts.append("</div>")  # review-item

                                for cm in parent_comments:
                                    render_comment(cm, is_reply=False)
                                    child_replies = replies_by_parent.get(cm.id, [])
                                    if child_replies:
                                        html_parts.append(
                                            "<div class='review-replies'>"
                                        )
                                        for rp in child_replies:
                                            render_comment(rp, is_reply=True)
                                        html_parts.append("</div>")

                                html_parts.append("</div>")  # reviewer-group-body
                                html_parts.append("</details>")  # reviewer-group

                            # 4. 孤立回复：没有匹配主评论的回复，单独展示
                            if orphan_replies:
                                html_parts.append("<details class='reviewer-group' open>")
                                html_parts.append("<summary>")
                                html_parts.append(
                                    "<div class='reviewer-group-title'>"
                                    "回复（无主）"
                                    f"<span>{len(orphan_replies)} 条回复</span>"
                                    "</div>"
                                )
                                html_parts.append("<div class='reviewer-chevron'>▶</div>")
                                html_parts.append("</summary>")
                                html_parts.append("<div class='reviewer-group-body'>")
                                html_parts.append("<div class='review-replies'>")
                                for rp in orphan_replies:
                                    render_comment(rp, is_reply=True)
                                html_parts.append("</div>")
                                html_parts.append("</div>")
                                html_parts.append("</details>")

                        html_parts.append(
                            "<div class='empty-text' data-empty-unresolved style='display:none'>无未解决的检视意见</div>"
                        )
                        html_parts.append("</div>")  # reviews wrapper

                        html_parts.append("</div>")  # pr-card
                    html_parts.append("</div>")  # pr-grid

                html_parts.append("</div>")  # user-content
                html_parts.append("</details>")  # user-block

            html_parts.append("</div>")  # repo-content
            html_parts.append("</details>")  # repo-block
    html_parts.append("</div>")  # card-view 容器

    # 列表视图容器
    html_parts.append("<div class='list-view' id='list-view'>")
    html_parts.append(
        "<table class='list-table' id='list-table'>"
        "<thead><tr>"
        "<th>仓库</th><th>用户</th><th>PR</th><th>状态</th><th>类型</th><th>未解决</th><th>已解决</th><th>新增</th><th>删除</th><th>文件</th><th>后缀</th><th>创建</th><th>更新时间</th><th>分支</th>"
        "</tr></thead>"
        "<tbody></tbody>"
        "</table>"
    )
    html_parts.append("</div>")

    # 检视意见视图容器（按提出人聚合，仅统计主评论）
    html_parts.append("<div class='issue-view' id='issue-view'>")
    html_parts.append(
        "<table class='list-table' id='issue-table'>"
        "<thead><tr>"
        "<th>提出人</th><th>检视意见（主评论）</th><th>未解决</th><th>已解决</th>"
        "</tr></thead>"
        "<tbody></tbody>"
        "</table>"
    )
    html_parts.append("</div>")

    # 被提检视意见视图容器（按 PR 作者聚合，仅统计主评论）
    html_parts.append("<div class='received-view' id='received-view'>")
    html_parts.append(
        "<table class='list-table' id='received-table'>"
        "<thead><tr>"
        "<th>被提人（PR 作者）</th><th>检视意见（主评论）</th><th>未解决</th><th>已解决</th>"
        "</tr></thead>"
        "<tbody></tbody>"
        "</table>"
    )
    html_parts.append("</div>")

    # 代码量统计视图（按 PR 作者聚合）
    html_parts.append("<div class='code-view' id='code-view'>")
    html_parts.append(
        "<table class='list-table' id='code-table'>"
        "<thead><tr>"
        "<th>用户</th><th>PR 数</th><th>新增</th><th>删除</th><th>文件</th>"
        "<th>cj</th><th>c/cpp</th><th>h</th><th>md</th>"
        "</tr></thead>"
        "<tbody></tbody>"
        "</table>"
    )
    html_parts.append("</div>")

    script = """
<script>
(() => {
  const filterUnresolved = document.getElementById('filter-unresolved');
  const filterHideClean = document.getElementById('filter-hide-clean');
  const filterHideEmptyUsers = document.getElementById('filter-hide-empty-users');
  const filterCommentKeyword = document.getElementById('filter-comment-keyword');
  const filterCommentExclude = document.getElementById('filter-comment-exclude');
  const filterHideReplies = document.getElementById('filter-hide-replies');
  const filterDateField = document.getElementById('filter-date-field');
  const filterResolvedOnly = document.getElementById('filter-resolved-only');
  const filterDateStart = document.getElementById('filter-date-start');
  const filterDateEnd = document.getElementById('filter-date-end');
  const fetchDateField = document.getElementById('fetch-date-field');
  const fetchDateStart = document.getElementById('fetch-date-start');
  const fetchDateEnd = document.getElementById('fetch-date-end');
  const fetchDateClear = document.getElementById('fetch-date-clear');
  const filterBar = document.getElementById('filter-bar');
  const filterToggle = document.getElementById('filter-toggle');
  const filterSummary = document.getElementById('filter-summary');
  const settingsModal = document.getElementById('settings-modal');
  const settingsBackdrop = document.getElementById('settings-backdrop');
  const settingsCloseBtn = document.getElementById('settings-close');
  const fetchModal = document.getElementById('fetch-modal');
  const fetchBackdrop = document.getElementById('fetch-backdrop');
  const fetchCloseBtn = document.getElementById('fetch-close');
  const headerMenuBtn = document.getElementById('header-menu-btn');
  const themeSelect = document.getElementById('theme-select');
  const sortSelect = document.getElementById('sort-select');
  const quickOpenUnresolvedBtn = document.getElementById('quick-open-unresolved');
  const cardView = document.getElementById('card-view');
  const listView = document.getElementById('list-view');
  const listTableBody = document.querySelector('#list-table tbody');
  const issueView = document.getElementById('issue-view');
  const issueTableBody = document.querySelector('#issue-table tbody');
  const receivedView = document.getElementById('received-view');
  const receivedTableBody = document.querySelector('#received-table tbody');
  const codeView = document.getElementById('code-view');
  const codeTableBody = document.querySelector('#code-table tbody');
  const viewCardBtn = document.getElementById('view-card-btn');
  const viewListBtn = document.getElementById('view-list-btn');
  const viewIssueBtn = document.getElementById('view-issue-btn');
  const viewReceivedBtn = document.getElementById('view-received-btn');
  const viewCodeBtn = document.getElementById('view-code-btn');
  const reviewControls = document.getElementById('review-controls');
  const reviewUserKeyword = document.getElementById('review-user-keyword');
  const reviewSortSelect = document.getElementById('review-sort-select');
  const exportReviewBtn = document.getElementById('export-review-csv');
  const presetSelect = document.getElementById('preset-select');
  const presetApplyBtn = document.getElementById('preset-apply');
  const presetSaveBtn = document.getElementById('preset-save');
  const fetchToggle = document.getElementById('fetch-toggle');
  const tokenInput = document.getElementById('api-token');
  const tokenClearBtn = document.getElementById('token-clear');
  const tokenStatus = document.getElementById('token-status');
  const refreshStatus = document.getElementById('refresh-status');
  const fetchModeSelect = document.getElementById('fetch-mode-select');
  const refreshBtn = document.getElementById('refresh-data');
  const statTotalMini = document.getElementById('stat-total-mini');
  const statOpenMini = document.getElementById('stat-open-mini');
  const statMergedMini = document.getElementById('stat-merged-mini');
  const statUnresolvedMini = document.getElementById('stat-unresolved-mini');
  const refreshStamp = document.getElementById('refresh-stamp');
  const fetchStateChecks = Array.from(document.querySelectorAll('.fetch-state-checkbox'));
  const stateChecks = Array.from(document.querySelectorAll('.filter-state-checkbox'));
  const commentChecks = Array.from(document.querySelectorAll('.filter-comment-checkbox'));
  let issueLabelChecks = Array.from(document.querySelectorAll('.filter-issue-label-checkbox'));
  let prTypeChecks = Array.from(document.querySelectorAll('.filter-pr-type-checkbox'));
  let targetChecks = Array.from(document.querySelectorAll('.filter-target-checkbox'));
  let userChecks = Array.from(document.querySelectorAll('.filter-user-checkbox'));
  const userSelectAllBtn = document.getElementById('filter-user-all');
  const userSelectNoneBtn = document.getElementById('filter-user-none');
  const userToggle = document.getElementById('filter-user-toggle');
  const userPanel = document.getElementById('filter-user-panel');
  const userDropdown = document.getElementById('filter-user-dropdown');
  const userList = document.getElementById('filter-user-list');
  const issueGroup = document.getElementById('filter-issue-group');
  const issueList = document.getElementById('filter-issue-list');
  const prTypeGroup = document.getElementById('filter-pr-type-group');
  const prTypeList = document.getElementById('filter-pr-type-list');
  const targetGroup = document.getElementById('filter-target-group');
  const targetList = document.getElementById('filter-target-list');
  let groupChecks = Array.from(document.querySelectorAll('.filter-group-checkbox'));
  const groupSelectAllBtn = document.getElementById('filter-group-all');
  const groupSelectNoneBtn = document.getElementById('filter-group-none');
  const groupToggle = document.getElementById('filter-group-toggle');
  const groupPanel = document.getElementById('filter-group-panel');
  const groupDropdown = document.getElementById('filter-group-dropdown');
  const groupList = document.getElementById('filter-group-list');
  const configPanel = document.getElementById('client-config-panel');
  const configUsers = document.getElementById('config-users');
  const configGroups = document.getElementById('config-groups');
  const configApplyBtn = document.getElementById('config-apply');
  const configResetBtn = document.getElementById('config-reset');
  const tuningIntervalInput = document.getElementById('config-request-interval');
  const tuningRepoInput = document.getElementById('config-repo-concurrency');
  const tuningDetailInput = document.getElementById('config-detail-concurrency');
  const tuningApplyBtn = document.getElementById('config-tuning-apply');
  const tuningResetBtn = document.getElementById('config-tuning-reset');
  let wrappedApply = () => {};
  let bindUserGroupListeners = () => {};
  const THEME_KEY = 'pr_report_theme_v1';
  const normalizeTheme = (v) => {
    const t = (v || '').toString();
    return ['dark', 'dim', 'light', 'contrast'].includes(t) ? t : 'dark';
  };
  const applyTheme = (t) => {
    document.documentElement.dataset.theme = normalizeTheme(t);
  };
  try {
    const saved = normalizeTheme(localStorage.getItem(THEME_KEY) || '');
    applyTheme(saved);
    if (themeSelect) themeSelect.value = saved;
  } catch (e) {
    applyTheme('dark');
  }
  if (themeSelect) {
    themeSelect.addEventListener('change', () => {
      const t = normalizeTheme(themeSelect.value);
      applyTheme(t);
      try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    });
  }

  const CLIENT_CONFIG = __CLIENT_CONFIG__;
  const TOKEN_KEY = 'gitcode_api_token_v1';
  const API_BASE_URL = CLIENT_CONFIG.baseUrl || 'https://api.gitcode.com/api/v5';
  const ALLOWED_PR_TYPES = new Set(CLIENT_CONFIG.allowedPrTypes || []);
  const CODE_STATS_ENABLED = CLIENT_CONFIG.codeStatsEnabled !== false;
  const CODE_STAT_SUFFIXES = new Set(
    (CLIENT_CONFIG.codeStatSuffixes || []).map((s) => (s || '').toLowerCase())
  );
  const CODE_STAT_COLUMNS = [
    { label: 'cj', exts: ['.cj'] },
    { label: 'c/cpp', exts: ['.c', '.cpp'] },
    { label: 'h', exts: ['.h'] },
    { label: 'md', exts: ['.md'] },
  ];
  const DEFAULT_REQUEST_INTERVAL_MS = Math.max(
    500,
    parseInt(CLIENT_CONFIG.requestIntervalMs || '1200', 10) || 1200
  );
  const DEFAULT_REPO_CONCURRENCY = 4;
  const DEFAULT_DETAIL_CONCURRENCY = 6;
  let baseRequestIntervalMs = DEFAULT_REQUEST_INTERVAL_MS;
  let requestIntervalMs = baseRequestIntervalMs;
  let successStreak = 0;
  let repoConcurrency = DEFAULT_REPO_CONCURRENCY;
  let detailConcurrency = DEFAULT_DETAIL_CONCURRENCY;
  let groupMembers = __GROUP_MEMBERS__;
  const logInfo = (...args) => {
    if (typeof console !== 'undefined' && console.info) {
      console.info('[pr-report]', ...args);
    }
  };
  try {
    const savedStamp = localStorage.getItem('pr_report_last_refresh_v1') || '';
    if (savedStamp && refreshStamp) {
      refreshStamp.textContent = `刷新：${savedStamp}`;
    }
  } catch (e) {}
  const FETCH_MODE_KEY = 'pr_report_fetch_mode_v1';
  const normalizeFetchMode = (val) =>
    ['none', 'details', 'api'].includes(val) ? val : 'none';
  if (fetchModeSelect) {
    const saved = normalizeFetchMode(localStorage.getItem(FETCH_MODE_KEY) || '');
    fetchModeSelect.value = saved;
    fetchModeSelect.addEventListener('change', () => {
      const v = normalizeFetchMode(fetchModeSelect.value || '');
      try { localStorage.setItem(FETCH_MODE_KEY, v); } catch (e) {}
    });
  }
  const FETCH_STATE_KEY = 'pr_report_fetch_state_v1';
  const normalizeFetchStates = (input) => {
    const allowed = new Set(['open', 'merged', 'closed', 'all', 'locked']);
    const list = Array.isArray(input) ? input : [];
    return list
      .map((val) => (val || '').toString().toLowerCase().trim())
      .filter((val) => allowed.has(val));
  };
  const getFetchStates = () => {
    if (!fetchStateChecks.length) return [];
    const selected = fetchStateChecks.filter((c) => c.checked).map((c) => c.value || '');
    return normalizeFetchStates(selected);
  };
  const applyFetchStateUi = (states) => {
    if (!fetchStateChecks.length) return;
    const set = new Set(normalizeFetchStates(states));
    fetchStateChecks.forEach((c) => {
      c.checked = set.has(c.value);
    });
  };
  const saveFetchStates = () => {
    try {
      localStorage.setItem(FETCH_STATE_KEY, JSON.stringify(getFetchStates()));
    } catch (e) {}
  };
  if (fetchStateChecks.length) {
    try {
      const raw = localStorage.getItem(FETCH_STATE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length) {
          applyFetchStateUi(parsed);
        }
      }
    } catch (e) {}
    fetchStateChecks.forEach((c) => c.addEventListener('change', saveFetchStates));
  }
  const FETCH_RANGE_KEY = 'pr_report_fetch_range_v1';
  const DATA_CACHE_KEY = 'pr_report_cached_data_v1';
  const loadFetchRange = () => {
    try {
      const raw = localStorage.getItem(FETCH_RANGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return {
        field: parsed.field === 'updated' ? 'updated' : 'created',
        start: parsed.start || '',
        end: parsed.end || '',
      };
    } catch (e) {
      return null;
    }
  };
  const saveFetchRange = () => {
    if (!fetchDateField || !fetchDateStart || !fetchDateEnd) return;
    const payload = {
      field: fetchDateField.value === 'updated' ? 'updated' : 'created',
      start: fetchDateStart.value || '',
      end: fetchDateEnd.value || '',
    };
    try { localStorage.setItem(FETCH_RANGE_KEY, JSON.stringify(payload)); } catch (e) {}
  };
  const syncFetchRangeUi = () => {
    const saved = loadFetchRange();
    if (!saved) return;
    if (fetchDateField) fetchDateField.value = saved.field || 'created';
    if (fetchDateStart) fetchDateStart.value = saved.start || '';
    if (fetchDateEnd) fetchDateEnd.value = saved.end || '';
  };
  syncFetchRangeUi();
  if (fetchDateField) fetchDateField.addEventListener('change', saveFetchRange);
  if (fetchDateStart) fetchDateStart.addEventListener('change', saveFetchRange);
  if (fetchDateEnd) fetchDateEnd.addEventListener('change', saveFetchRange);
  if (fetchDateClear) {
    fetchDateClear.addEventListener('click', () => {
      if (fetchDateStart) fetchDateStart.value = '';
      if (fetchDateEnd) fetchDateEnd.value = '';
      saveFetchRange();
    });
  }
  const saveCachedData = (data) => {
    try {
      const json = JSON.stringify(data || {});
      if (json.length > 4500000) {
        logInfo('缓存过大，跳过保存', json.length);
        localStorage.removeItem(DATA_CACHE_KEY);
        return;
      }
      localStorage.setItem(DATA_CACHE_KEY, json);
    } catch (e) {
      logInfo('缓存保存失败', e?.message || e);
    }
  };
  const loadCachedData = () => {
    try {
      const raw = localStorage.getItem(DATA_CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return parsed;
    } catch (e) {
      return null;
    }
  };

  const readToken = () => {
    try {
      return localStorage.getItem(TOKEN_KEY) || '';
    } catch (e) {
      return '';
    }
  };
  const saveToken = (token) => {
    try {
      if (token) {
        localStorage.setItem(TOKEN_KEY, token);
      } else {
        localStorage.removeItem(TOKEN_KEY);
      }
    } catch (e) {}
  };
  const setTokenStatus = (token, state = '') => {
    if (!tokenStatus) return;
    tokenStatus.textContent = token ? 'Token 已保存' : '未设置';
    tokenStatus.dataset.state = state || (token ? 'ok' : '');
  };
  const setRefreshStatus = (text, state = '') => {
    if (!refreshStatus) return;
    refreshStatus.textContent = text || '';
    refreshStatus.dataset.state = state || '';
  };
  const initTokenUi = () => {
    const saved = readToken();
    if (tokenInput && saved) {
      tokenInput.value = saved;
    }
    setTokenStatus(saved);
  };
  initTokenUi();
  if (tokenClearBtn) {
    tokenClearBtn.addEventListener('click', () => {
      if (tokenInput) tokenInput.value = '';
      saveToken('');
      setTokenStatus('');
      setRefreshStatus('');
    });
  }

  const FETCH_TUNING_KEY = 'pr_report_fetch_tuning_v1';
  const clampInt = (raw, min, max, fallback) => {
    const num = parseInt(raw, 10);
    if (Number.isNaN(num)) return fallback;
    return Math.min(Math.max(num, min), max);
  };
  const loadFetchTuning = () => {
    try {
      const raw = localStorage.getItem(FETCH_TUNING_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return parsed;
    } catch (e) {
      return null;
    }
  };
  const saveFetchTuning = (tuning) => {
    try {
      if (!tuning) {
        localStorage.removeItem(FETCH_TUNING_KEY);
        return;
      }
      localStorage.setItem(FETCH_TUNING_KEY, JSON.stringify(tuning));
    } catch (e) {}
  };
  const applyFetchTuning = (tuning, opts = {}) => {
    const next = {
      requestIntervalMs: clampInt(
        tuning?.requestIntervalMs,
        500,
        8000,
        DEFAULT_REQUEST_INTERVAL_MS
      ),
      repoConcurrency: clampInt(
        tuning?.repoConcurrency,
        1,
        6,
        DEFAULT_REPO_CONCURRENCY
      ),
      detailConcurrency: clampInt(
        tuning?.detailConcurrency,
        1,
        10,
        DEFAULT_DETAIL_CONCURRENCY
      ),
    };
    baseRequestIntervalMs = next.requestIntervalMs;
    requestIntervalMs = next.requestIntervalMs;
    repoConcurrency = next.repoConcurrency;
    detailConcurrency = next.detailConcurrency;
    successStreak = 0;
    if (opts?.updateUi !== false) {
      if (tuningIntervalInput) tuningIntervalInput.value = String(next.requestIntervalMs);
      if (tuningRepoInput) tuningRepoInput.value = String(next.repoConcurrency);
      if (tuningDetailInput) tuningDetailInput.value = String(next.detailConcurrency);
    }
    if (opts?.persist) {
      saveFetchTuning(next);
    }
    logInfo('抓取设置', next);
  };
  applyFetchTuning(loadFetchTuning() || {}, { updateUi: true });
  if (tuningApplyBtn) {
    tuningApplyBtn.addEventListener('click', () => {
      const tuning = {
        requestIntervalMs: tuningIntervalInput?.value,
        repoConcurrency: tuningRepoInput?.value,
        detailConcurrency: tuningDetailInput?.value,
      };
      applyFetchTuning(tuning, { persist: true, updateUi: true });
    });
  }
  if (tuningResetBtn) {
    tuningResetBtn.addEventListener('click', () => {
      saveFetchTuning(null);
      applyFetchTuning({}, { updateUi: true });
    });
  }

  const CLIENT_CFG_KEY = 'pr_report_client_cfg_v1';
  const normalizeUsers = (text) => {
    const raw = (text || '').split(/[\\n,]/);
    const seen = new Set();
    const list = [];
    raw.forEach((item) => {
      const name = (item || '').trim();
      if (!name || seen.has(name)) return;
      seen.add(name);
      list.push(name);
    });
    return list;
  };
  const parseGroups = (text) => {
    const groups = {};
    (text || '').split(/\\n/).forEach((line) => {
      const trimmed = (line || '').trim();
      if (!trimmed) return;
      const idx = trimmed.indexOf(':');
      if (idx <= 0) return;
      const name = trimmed.slice(0, idx).trim();
      const usersRaw = trimmed.slice(idx + 1).trim();
      if (!name) return;
      const users = normalizeUsers(usersRaw.replace(/\\s+/g, ' '));
      groups[name] = users;
    });
    return groups;
  };
  const formatGroups = (groups) => {
    if (!groups) return '';
    return Object.entries(groups)
      .map(([name, users]) => `${name}: ${(users || []).join(', ')}`.trim())
      .join('\\n');
  };
  const loadClientConfig = () => {
    try {
      const raw = localStorage.getItem(CLIENT_CFG_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return {
        users: Array.isArray(parsed.users) ? parsed.users : [],
        groups: parsed.groups && typeof parsed.groups === 'object' ? parsed.groups : {},
        usersSet: parsed.usersSet === true,
        groupsSet: parsed.groupsSet === true,
      };
    } catch (e) {
      return null;
    }
  };
  const saveClientConfig = (cfg) => {
    try {
      if (!cfg) {
        localStorage.removeItem(CLIENT_CFG_KEY);
        return;
      }
      localStorage.setItem(CLIENT_CFG_KEY, JSON.stringify(cfg));
    } catch (e) {}
  };
  let clientConfigState = loadClientConfig();
  const getEffectiveUsers = () => {
    if (clientConfigState && clientConfigState.usersSet) {
      return clientConfigState.users || [];
    }
    return Array.isArray(CLIENT_CONFIG.users) ? CLIENT_CONFIG.users : [];
  };
  const getEffectiveGroups = () => {
    if (clientConfigState && clientConfigState.groupsSet) {
      return clientConfigState.groups || {};
    }
    return CLIENT_CONFIG.groups || {};
  };
  const renderUserGroupFilters = (users, groups) => {
    if (userList) {
      userList.innerHTML = '';
      if (!users.length) {
        userList.innerHTML = "<div class='empty-text'>未配置用户</div>";
      } else {
        users.forEach((uname) => {
          const label = document.createElement('label');
          label.className = 'filter-user-item';
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.className = 'filter-user-checkbox';
          input.value = uname;
          input.checked = true;
          label.appendChild(input);
          label.append(` ${uname}`);
          userList.appendChild(label);
        });
      }
      userChecks = Array.from(userList.querySelectorAll('.filter-user-checkbox'));
      if (userDropdown) userDropdown.style.display = users.length ? '' : 'none';
    }

    if (groupList) {
      groupList.innerHTML = '';
      const groupNames = Object.keys(groups || {});
      if (!groupNames.length) {
        groupList.innerHTML = "<div class='empty-text'>未配置用户组</div>";
      } else {
        groupNames.forEach((name) => {
          const label = document.createElement('label');
          label.className = 'filter-user-item';
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.className = 'filter-group-checkbox';
          input.value = name;
          input.checked = true;
          label.appendChild(input);
          label.append(` ${name}`);
          const members = groups[name] || [];
          if (members.length) {
            const span = document.createElement('span');
            span.className = 'group-members';
            span.textContent = ` ( ${members.join(', ')} )`;
            label.appendChild(span);
          }
          groupList.appendChild(label);
        });
      }
      groupChecks = Array.from(groupList.querySelectorAll('.filter-group-checkbox'));
      if (groupDropdown) groupDropdown.style.display = groupChecks.length ? '' : 'none';
    }
  };
  const applyClientConfig = () => {
    const users = getEffectiveUsers();
    const groups = getEffectiveGroups();
    groupMembers = groups || {};
    if (configUsers) configUsers.value = users.join('\\n');
    if (configGroups) configGroups.value = formatGroups(groups);
    renderUserGroupFilters(users, groups);
    bindUserGroupListeners();
  };
  applyClientConfig();
  if (configApplyBtn) {
    configApplyBtn.addEventListener('click', () => {
      const users = normalizeUsers(configUsers ? configUsers.value : '');
      const groups = parseGroups(configGroups ? configGroups.value : '');
      clientConfigState = {
        users,
        groups,
        usersSet: true,
        groupsSet: true,
      };
      saveClientConfig(clientConfigState);
      applyClientConfig();
      refreshFromApi();
    });
  }
  if (configResetBtn) {
    configResetBtn.addEventListener('click', () => {
      clientConfigState = { users: [], groups: {}, usersSet: false, groupsSet: false };
      saveClientConfig(null);
      applyClientConfig();
      refreshFromApi();
    });
  }

  if (!filterUnresolved || !filterHideClean) return;

  const getSelectedUsers = () => {
    if (!userChecks.length) return null;
    return new Set(
      userChecks.filter((c) => c.checked).map((c) => c.value || '')
    );
  };

  const getSelectedGroups = () => {
    if (!groupChecks.length) return null;
    return new Set(
      groupChecks.filter((c) => c.checked).map((c) => c.value || '')
    );
  };

  const getSelectedStates = () => {
    const checked = stateChecks.filter((c) => c.checked).map((c) => c.value);
    return new Set(checked.length ? checked : ['open', 'merged']);
  };

  const getSelectedCommentKinds = () => {
    const checked = commentChecks.filter((c) => c.checked).map((c) => c.value);
    return new Set(checked.length ? checked : ['unresolved', 'resolved', 'none']);
  };

  const getSortKey = () => (sortSelect ? sortSelect.value : 'created');

  const getSelectedIssueLabels = () => {
    if (!issueLabelChecks.length) return new Set();
    const checked = issueLabelChecks.filter((c) => c.checked).map((c) => c.value);
    return new Set(checked);
  };

  const getSelectedPrTypes = () => {
    if (!prTypeChecks.length) return new Set();
    const checked = prTypeChecks.filter((c) => c.checked).map((c) => c.value);
    return new Set(checked);
  };

  const getSelectedTargets = () => {
    if (!targetChecks.length) return new Set();
    const checked = targetChecks.filter((c) => c.checked).map((c) => c.value);
    return new Set(checked);
  };

  const collectVisibleCards = () => {
    const rows = [];
    const cards = Array.from(document.querySelectorAll('.pr-card'));
    cards.forEach((card) => {
      const userBlock = card.closest('[data-user-block]');
      if (card.style.display === 'none') return;
      if (userBlock && userBlock.style.display === 'none') return;
      const repo = card.dataset.repo || '';
      const user = card.dataset.username || '';
      const num = card.dataset.prNumber || '';
      const title = card.dataset.title || '';
      const url = card.dataset.url || '';
      const state = card.dataset.state || '';
      const unresolved = parseInt(card.dataset.unresolvedCount || '0', 10) || 0;
      const resolved = parseInt(card.dataset.resolvedCount || '0', 10) || 0;
      const codeKnown = (card.dataset.codeKnown || '') === '1';
      const additionsRaw = card.dataset.additions || '';
      const deletionsRaw = card.dataset.deletions || '';
      const filesRaw = card.dataset.changedFiles || '';
      const additions = parseInt(additionsRaw || '0', 10) || 0;
      const deletions = parseInt(deletionsRaw || '0', 10) || 0;
      const files = parseInt(filesRaw || '0', 10) || 0;
      const codeStats = parseCodeStats(card.dataset.codeStats || '');
      const extSummary = summarizeExtStats(codeStats);
      const created = card.dataset.created || '';
      const updated = card.dataset.updated || '';
      const branch =
        card.dataset.source || card.dataset.target
          ? `${card.dataset.source || ''} → ${card.dataset.target || ''}`
          : '';
      const labelsRaw = card.dataset.issueLabels || '';
      const labels = labelsRaw ? labelsRaw.split('||').filter(Boolean) : [];
      const type = card.dataset.prType || '';
      rows.push({
        repo,
        user,
        num,
        title,
        url,
        state,
        unresolved,
        resolved,
        additions,
        deletions,
        files,
        codeKnown,
        extSummary,
        codeStats,
        created,
        updated,
        branch,
        labels,
        type,
      });
    });
    return rows;
  };

  const refreshListView = () => {
    if (!listTableBody) return;
    listTableBody.innerHTML = '';
    const rows = collectVisibleCards();
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      const prCell = r.url
        ? `<a href="${r.url}" target="_blank" rel="noopener noreferrer">#${r.num} ${r.title}</a>`
        : `#${r.num} ${r.title}`;
      const addDisplay = r.codeKnown ? r.additions : '-';
      const delDisplay = r.codeKnown ? r.deletions : '-';
      const filesDisplay = r.codeKnown ? r.files : '-';
      const extDisplay = r.codeKnown ? (r.extSummary || '-') : '-';
      tr.innerHTML = `
        <td>${r.repo}</td>
        <td>${r.user}</td>
        <td>${prCell}</td>
        <td>${r.state}</td>
        <td>${r.type || ''}</td>
        <td>${r.unresolved}</td>
        <td>${r.resolved}</td>
        <td>${addDisplay}</td>
        <td>${delDisplay}</td>
        <td>${filesDisplay}</td>
        <td>${extDisplay}</td>
        <td>${r.created}</td>
        <td>${r.updated}</td>
        <td>${r.branch}</td>
      `;
      listTableBody.appendChild(tr);
    });
  };

  // 检视意见视图：按“提出检视意见的人（主评论作者）”聚合
  let issueDetailMap = new Map();
  const collectVisibleIssues = () => {
    const rows = [];
    const cards = Array.from(document.querySelectorAll('.pr-card'));
    const toExcerpt = (text) => {
      const compact = (text || '').replace(/\\s+/g, ' ').trim();
      if (!compact) return '';
      return compact.length > 120 ? compact.slice(0, 120) + '…' : compact;
    };
    cards.forEach((card) => {
      const userBlock = card.closest('[data-user-block]');
      if (card.style.display === 'none') return;
      if (userBlock && userBlock.style.display === 'none') return;
      const repo = card.dataset.repo || '';
      const prNum = card.dataset.prNumber || '';
      const prTitle = card.dataset.title || '';
      const prUrl = card.dataset.url || '';
      const prAuthor = (card.dataset.username || '').trim() || '(unknown)';
      const items = Array.from(
        card.querySelectorAll('.review-item[data-is-reply="0"]')
      );
      items.forEach((it) => {
        if (it.style.display === 'none') return;
        const user = (it.dataset.user || '').trim();
        const resolved = it.dataset.resolved === 'true';
        const commentId = (it.dataset.commentId || '').trim();
        const bodyNode =
          it.querySelector('.review-body') || it.querySelector('.review-body-content');
        const excerpt = toExcerpt(bodyNode ? bodyNode.textContent : '');
        rows.push({ repo, prNum, prTitle, prUrl, prAuthor, user, resolved, commentId, excerpt });
      });
    });
    return rows;
  };

  const refreshIssueView = () => {
    if (!issueTableBody) return;
    issueTableBody.innerHTML = '';
    const rows = collectVisibleIssues();
    const map = new Map();
    issueDetailMap = new Map();
    rows.forEach((r) => {
      const key = r.user || '(unknown)';
      if (!map.has(key)) {
        map.set(key, { user: key, total: 0, unresolved: 0, resolved: 0 });
      }
      if (!issueDetailMap.has(key)) {
        issueDetailMap.set(key, []);
      }
      issueDetailMap.get(key).push(r);
      const acc = map.get(key);
      acc.total += 1;
      if (r.resolved) acc.resolved += 1;
      else acc.unresolved += 1;
    });
    const kw = getReviewKeyword();
    const sortKey = getReviewSortKey();
    const list = Array.from(map.values())
      .filter((it) => (!kw ? true : (it.user || '').toLowerCase().includes(kw)))
      .sort((a, b) => reviewSortCompare(a, b, sortKey));
    if (!list.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.textContent = '当前筛选下无检视意见（主评论）';
      tr.appendChild(td);
      issueTableBody.appendChild(tr);
      return;
    }
    list.forEach((r) => {
      const tr = document.createElement('tr');
      const tdUser = document.createElement('td');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'issue-toggle';
      btn.dataset.issueToggle = '1';
      btn.dataset.user = r.user;
      btn.dataset.label = r.user;
      btn.setAttribute('aria-expanded', 'false');
      btn.textContent = `▸ ${r.user}`;
      tdUser.appendChild(btn);
      const tdTotal = document.createElement('td');
      tdTotal.textContent = String(r.total);
      const tdUnresolved = document.createElement('td');
      tdUnresolved.textContent = String(r.unresolved);
      const tdResolved = document.createElement('td');
      tdResolved.textContent = String(r.resolved);
      tr.appendChild(tdUser);
      tr.appendChild(tdTotal);
      tr.appendChild(tdUnresolved);
      tr.appendChild(tdResolved);
      issueTableBody.appendChild(tr);
    });
  };

  if (issueTableBody) {
    issueTableBody.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-issue-toggle]');
      if (!btn) return;
      const userKey = btn.dataset.user || '(unknown)';
      const row = btn.closest('tr');
      if (!row) return;

      const next = row.nextElementSibling;
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      if (isOpen) {
        if (next && next.classList.contains('issue-detail-row')) {
          next.remove();
        }
        btn.setAttribute('aria-expanded', 'false');
        const label = btn.dataset.label || userKey;
        btn.textContent = `▸ ${label}`;
        return;
      }
      if (next && next.classList.contains('issue-detail-row')) {
        next.remove();
      }

      const details = issueDetailMap.get(userKey) || [];
      const sorted = [...details].sort((a, b) => {
        if (a.resolved !== b.resolved) return a.resolved ? 1 : -1; // unresolved first
        const ra = a.repo || '';
        const rb = b.repo || '';
        const cmpRepo = ra.localeCompare(rb);
        if (cmpRepo) return cmpRepo;
        const na = parseInt(a.prNum || '0', 10) || 0;
        const nb = parseInt(b.prNum || '0', 10) || 0;
        if (na !== nb) return na - nb;
        return (a.commentId || '').localeCompare(b.commentId || '');
      });

      const detailTr = document.createElement('tr');
      detailTr.className = 'issue-detail-row';
      const detailTd = document.createElement('td');
      detailTd.colSpan = 4;
      const wrap = document.createElement('div');
      wrap.className = 'issue-detail';

      if (!sorted.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-text';
        empty.textContent = '无检视意见（主评论）';
        wrap.appendChild(empty);
      } else {
        sorted.forEach((it) => {
          const line = document.createElement('div');
          line.className = 'issue-detail-item';

          const pill = document.createElement('span');
          pill.className = `issue-pill ${it.resolved ? 'issue-pill-resolved' : 'issue-pill-unresolved'}`;
          pill.textContent = it.resolved ? '已解决' : '未解决';

          const link = document.createElement(it.prUrl ? 'a' : 'span');
          if (it.prUrl) {
            link.href = it.prUrl;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.className = 'issue-detail-link';
          }
          const prPrefix = it.repo ? `${it.repo} ` : '';
          link.textContent = `${prPrefix}#${it.prNum || ''} ${it.prTitle || ''}`.trim();

          const meta = document.createElement('span');
          meta.className = 'issue-detail-meta';
          const cid = it.commentId ? `评论 #${it.commentId}` : '评论';
          if (it.prUrl && it.commentId) {
            const cLink = document.createElement('a');
            cLink.href = getCommentUrl(it.prUrl, it.commentId);
            cLink.target = '_blank';
            cLink.rel = 'noopener noreferrer';
            cLink.className = 'issue-detail-link';
            cLink.textContent = cid;
            meta.appendChild(cLink);
          } else {
            meta.textContent = cid;
          }
          if (it.excerpt) {
            const txt = document.createElement('span');
            txt.textContent = ` · ${it.excerpt}`;
            meta.appendChild(txt);
          }

          line.appendChild(pill);
          line.appendChild(link);
          line.appendChild(meta);
          wrap.appendChild(line);
        });
      }

      detailTd.appendChild(wrap);
      detailTr.appendChild(detailTd);
      row.after(detailTr);
      btn.setAttribute('aria-expanded', 'true');
      const label = btn.dataset.label || userKey;
      btn.textContent = `▾ ${label}`;
    });
  }

  // 被提检视意见视图：按“PR 作者”聚合（仅主评论）
  let receivedDetailMap = new Map();
  const collectVisibleReceived = () => {
    const rows = [];
    const cards = Array.from(document.querySelectorAll('.pr-card'));
    const toExcerpt = (text) => {
      const compact = (text || '').replace(/\\s+/g, ' ').trim();
      if (!compact) return '';
      return compact.length > 120 ? compact.slice(0, 120) + '…' : compact;
    };
    cards.forEach((card) => {
      const userBlock = card.closest('[data-user-block]');
      if (card.style.display === 'none') return;
      if (userBlock && userBlock.style.display === 'none') return;
      const repo = card.dataset.repo || '';
      const prNum = card.dataset.prNumber || '';
      const prTitle = card.dataset.title || '';
      const prUrl = card.dataset.url || '';
      const prAuthor = (card.dataset.username || '').trim() || '(unknown)';

      const items = Array.from(
        card.querySelectorAll('.review-item[data-is-reply="0"]')
      );
      items.forEach((it) => {
        if (it.style.display === 'none') return;
        const reviewer = (it.dataset.user || '').trim() || '(unknown)';
        const resolved = it.dataset.resolved === 'true';
        const commentId = (it.dataset.commentId || '').trim();
        const bodyNode =
          it.querySelector('.review-body') || it.querySelector('.review-body-content');
        const excerpt = toExcerpt(bodyNode ? bodyNode.textContent : '');
        rows.push({
          repo,
          prNum,
          prTitle,
          prUrl,
          prAuthor,
          reviewer,
          resolved,
          commentId,
          excerpt,
        });
      });
    });
    return rows;
  };

  const refreshReceivedView = () => {
    if (!receivedTableBody) return;
    receivedTableBody.innerHTML = '';
    const rows = collectVisibleReceived();
    const map = new Map();
    receivedDetailMap = new Map();
    rows.forEach((r) => {
      const key = r.prAuthor || '(unknown)';
      if (!map.has(key)) {
        map.set(key, { user: key, total: 0, unresolved: 0, resolved: 0 });
      }
      if (!receivedDetailMap.has(key)) {
        receivedDetailMap.set(key, []);
      }
      receivedDetailMap.get(key).push(r);
      const acc = map.get(key);
      acc.total += 1;
      if (r.resolved) acc.resolved += 1;
      else acc.unresolved += 1;
    });
    const kw = getReviewKeyword();
    const sortKey = getReviewSortKey();
    const list = Array.from(map.values())
      .filter((it) => (!kw ? true : (it.user || '').toLowerCase().includes(kw)))
      .sort((a, b) => reviewSortCompare(a, b, sortKey));
    if (!list.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 4;
      td.textContent = '当前筛选下无被提检视意见（主评论）';
      tr.appendChild(td);
      receivedTableBody.appendChild(tr);
      return;
    }
    list.forEach((r) => {
      const tr = document.createElement('tr');
      const tdUser = document.createElement('td');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'issue-toggle';
      btn.dataset.receivedToggle = '1';
      btn.dataset.user = r.user;
      btn.dataset.label = r.user;
      btn.setAttribute('aria-expanded', 'false');
      btn.textContent = `▸ ${r.user}`;
      tdUser.appendChild(btn);
      const tdTotal = document.createElement('td');
      tdTotal.textContent = String(r.total);
      const tdUnresolved = document.createElement('td');
      tdUnresolved.textContent = String(r.unresolved);
      const tdResolved = document.createElement('td');
      tdResolved.textContent = String(r.resolved);
      tr.appendChild(tdUser);
      tr.appendChild(tdTotal);
      tr.appendChild(tdUnresolved);
      tr.appendChild(tdResolved);
      receivedTableBody.appendChild(tr);
    });
  };

  if (receivedTableBody) {
    receivedTableBody.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-received-toggle]');
      if (!btn) return;
      const userKey = btn.dataset.user || '(unknown)';
      const row = btn.closest('tr');
      if (!row) return;

      const next = row.nextElementSibling;
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      if (isOpen) {
        if (next && next.classList.contains('received-detail-row')) {
          next.remove();
        }
        btn.setAttribute('aria-expanded', 'false');
        const label = btn.dataset.label || userKey;
        btn.textContent = `▸ ${label}`;
        return;
      }
      if (next && next.classList.contains('received-detail-row')) {
        next.remove();
      }

      const details = receivedDetailMap.get(userKey) || [];
      const sorted = [...details].sort((a, b) => {
        if (a.resolved !== b.resolved) return a.resolved ? 1 : -1; // unresolved first
        const ra = a.repo || '';
        const rb = b.repo || '';
        const cmpRepo = ra.localeCompare(rb);
        if (cmpRepo) return cmpRepo;
        const na = parseInt(a.prNum || '0', 10) || 0;
        const nb = parseInt(b.prNum || '0', 10) || 0;
        if (na !== nb) return na - nb;
        return (a.commentId || '').localeCompare(b.commentId || '');
      });

      const detailTr = document.createElement('tr');
      detailTr.className = 'received-detail-row';
      const detailTd = document.createElement('td');
      detailTd.colSpan = 4;
      const wrap = document.createElement('div');
      wrap.className = 'issue-detail';

      if (!sorted.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-text';
        empty.textContent = '无被提检视意见（主评论）';
        wrap.appendChild(empty);
      } else {
        sorted.forEach((it) => {
          const line = document.createElement('div');
          line.className = 'issue-detail-item';

          const pill = document.createElement('span');
          pill.className = `issue-pill ${it.resolved ? 'issue-pill-resolved' : 'issue-pill-unresolved'}`;
          pill.textContent = it.resolved ? '已解决' : '未解决';

          const link = document.createElement(it.prUrl ? 'a' : 'span');
          if (it.prUrl) {
            link.href = it.prUrl;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.className = 'issue-detail-link';
          }
          const prPrefix = it.repo ? `${it.repo} ` : '';
          link.textContent = `${prPrefix}#${it.prNum || ''} ${it.prTitle || ''}`.trim();

          const meta = document.createElement('span');
          meta.className = 'issue-detail-meta';
          const who = it.reviewer ? `提出人 ${it.reviewer}` : '';
          const cid = it.commentId ? `评论 #${it.commentId}` : '评论';
          const head = who ? `${who} · ${cid}` : cid;
          if (it.prUrl && it.commentId) {
            const cLink = document.createElement('a');
            cLink.href = getCommentUrl(it.prUrl, it.commentId);
            cLink.target = '_blank';
            cLink.rel = 'noopener noreferrer';
            cLink.className = 'issue-detail-link';
            cLink.textContent = head;
            meta.appendChild(cLink);
          } else {
            meta.textContent = head;
          }
          if (it.excerpt) {
            const txt = document.createElement('span');
            txt.textContent = ` · ${it.excerpt}`;
            meta.appendChild(txt);
          }

          line.appendChild(pill);
          line.appendChild(link);
          line.appendChild(meta);
          wrap.appendChild(line);
        });
      }

      detailTd.appendChild(wrap);
      detailTr.appendChild(detailTd);
      row.after(detailTr);
      btn.setAttribute('aria-expanded', 'true');
      const label = btn.dataset.label || userKey;
      btn.textContent = `▾ ${label}`;
    });
  }

  // 代码量统计视图：按 PR 作者聚合
  let codeDetailMap = new Map();
  const collectVisibleCodeRows = () => {
    const rows = [];
    const cards = Array.from(document.querySelectorAll('.pr-card'));
    cards.forEach((card) => {
      const userBlock = card.closest('[data-user-block]');
      if (card.style.display === 'none') return;
      if (userBlock && userBlock.style.display === 'none') return;
      const codeKnown = (card.dataset.codeKnown || '') === '1';
      if (!codeKnown) return;
      const repo = card.dataset.repo || '';
      const user = card.dataset.username || '';
      const num = card.dataset.prNumber || '';
      const title = card.dataset.title || '';
      const url = card.dataset.url || '';
      const additions = parseInt(card.dataset.additions || '0', 10) || 0;
      const deletions = parseInt(card.dataset.deletions || '0', 10) || 0;
      const files = parseInt(card.dataset.changedFiles || '0', 10) || 0;
      const codeStats = parseCodeStats(card.dataset.codeStats || '');
      rows.push({ repo, user, num, title, url, additions, deletions, files, codeStats });
    });
    return rows;
  };

  const refreshCodeView = () => {
    if (!codeTableBody) return;
    codeTableBody.innerHTML = '';
    const rows = collectVisibleCodeRows();
    const map = new Map();
    codeDetailMap = new Map();
    const codeColCount = 5 + CODE_STAT_COLUMNS.length;
    rows.forEach((r) => {
      const key = r.user || '(unknown)';
      if (!map.has(key)) {
        map.set(key, {
          user: key,
          prs: 0,
          additions: 0,
          deletions: 0,
          files: 0,
          extStats: {},
        });
      }
      if (!codeDetailMap.has(key)) {
        codeDetailMap.set(key, []);
      }
      codeDetailMap.get(key).push(r);
      const acc = map.get(key);
      acc.prs += 1;
      acc.additions += r.additions;
      acc.deletions += r.deletions;
      acc.files += r.files;
      if (r.codeStats) {
        Object.entries(r.codeStats).forEach(([ext, stat]) => {
          const bucket = acc.extStats[ext] || { additions: 0, deletions: 0, files: 0 };
          bucket.additions += parseInt(stat.additions || 0, 10) || 0;
          bucket.deletions += parseInt(stat.deletions || 0, 10) || 0;
          bucket.files += parseInt(stat.files || 0, 10) || 0;
          acc.extStats[ext] = bucket;
        });
      }
    });
    const list = Array.from(map.values()).sort((a, b) => {
      const ta = (a.additions || 0) + (a.deletions || 0);
      const tb = (b.additions || 0) + (b.deletions || 0);
      if (tb !== ta) return tb - ta;
      return (a.user || '').localeCompare(b.user || '');
    });
    if (!list.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = codeColCount;
      td.textContent = '当前筛选下无代码统计数据';
      tr.appendChild(td);
      codeTableBody.appendChild(tr);
      return;
    }
    list.forEach((r) => {
      const tr = document.createElement('tr');
      const tdUser = document.createElement('td');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'issue-toggle';
      btn.dataset.codeToggle = '1';
      btn.dataset.user = r.user;
      btn.dataset.label = r.user;
      btn.setAttribute('aria-expanded', 'false');
      btn.textContent = `▸ ${r.user}`;
      tdUser.appendChild(btn);
      const tdPrs = document.createElement('td');
      tdPrs.textContent = String(r.prs);
      const tdAdd = document.createElement('td');
      tdAdd.textContent = String(r.additions);
      const tdDel = document.createElement('td');
      tdDel.textContent = String(r.deletions);
      const tdFiles = document.createElement('td');
      tdFiles.textContent = String(r.files);
      tr.appendChild(tdUser);
      tr.appendChild(tdPrs);
      tr.appendChild(tdAdd);
      tr.appendChild(tdDel);
      tr.appendChild(tdFiles);
      CODE_STAT_COLUMNS.forEach((col) => {
        const td = document.createElement('td');
        const sums = sumExtStats(r.extStats, col.exts);
        td.textContent = sums.add || sums.del ? `+${sums.add}/-${sums.del}` : '-';
        tr.appendChild(td);
      });
      codeTableBody.appendChild(tr);
    });
  };

  if (codeTableBody) {
    codeTableBody.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-code-toggle]');
      if (!btn) return;
      const userKey = btn.dataset.user || '(unknown)';
      const row = btn.closest('tr');
      if (!row) return;

      const next = row.nextElementSibling;
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      if (isOpen) {
        if (next && next.classList.contains('code-detail-row')) {
          next.remove();
        }
        btn.setAttribute('aria-expanded', 'false');
        const label = btn.dataset.label || userKey;
        btn.textContent = `▸ ${label}`;
        return;
      }
      if (next && next.classList.contains('code-detail-row')) {
        next.remove();
      }

      const details = codeDetailMap.get(userKey) || [];
      const sorted = [...details].sort((a, b) => {
        const ta = (a.additions || 0) + (a.deletions || 0);
        const tb = (b.additions || 0) + (b.deletions || 0);
        if (tb !== ta) return tb - ta;
        return (a.repo || '').localeCompare(b.repo || '');
      });

      const detailTr = document.createElement('tr');
      detailTr.className = 'code-detail-row';
      const detailTd = document.createElement('td');
      detailTd.colSpan = 5 + CODE_STAT_COLUMNS.length;
      const wrap = document.createElement('div');
      wrap.className = 'issue-detail';

      if (!sorted.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-text';
        empty.textContent = '无代码统计记录';
        wrap.appendChild(empty);
      } else {
        sorted.forEach((it) => {
          const line = document.createElement('div');
          line.className = 'issue-detail-item';

          const link = document.createElement(it.url ? 'a' : 'span');
          if (it.url) {
            link.href = it.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.className = 'issue-detail-link';
          }
          const prPrefix = it.repo ? `${it.repo} ` : '';
          link.textContent = `${prPrefix}#${it.num || ''} ${it.title || ''}`.trim();

          const meta = document.createElement('span');
          meta.className = 'issue-detail-meta';
          const extSummary = summarizeExtStats(it.codeStats) || '';
          meta.textContent = `+${it.additions} / -${it.deletions} · 文件 ${it.files}${extSummary ? ` · ${extSummary}` : ''}`;

          line.appendChild(link);
          line.appendChild(meta);
          wrap.appendChild(line);
        });
      }

      detailTd.appendChild(wrap);
      detailTr.appendChild(detailTd);
      row.after(detailTr);
      btn.setAttribute('aria-expanded', 'true');
      const label = btn.dataset.label || userKey;
      btn.textContent = `▾ ${label}`;
    });
  }

  const refreshStats = () => {
    const rows = collectVisibleCards();
    const total = rows.length;
    const openCnt = rows.filter((r) => (r.state || '').toLowerCase() === 'open').length;
    const mergedCnt = rows.filter((r) => (r.state || '').toLowerCase() === 'merged').length;
    const unresolvedCnt = rows.filter((r) => r.unresolved > 0).length;
    if (statTotalMini) statTotalMini.textContent = total;
    if (statOpenMini) statOpenMini.textContent = openCnt;
    if (statMergedMini) statMergedMini.textContent = mergedCnt;
    if (statUnresolvedMini) statUnresolvedMini.textContent = unresolvedCnt;
  };

  // 预设
  const PRESET_KEY = 'pr_report_presets_v1';
  const loadPresets = () => {
    try {
      const raw = localStorage.getItem(PRESET_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  };
  const savePresets = (list) => {
    localStorage.setItem(PRESET_KEY, JSON.stringify(list || []));
  };
  const syncPresetOptions = () => {
    if (!presetSelect) return;
    const list = loadPresets();
    presetSelect.innerHTML = "<option value=''>预设：选择</option>";
    list.forEach((p, idx) => {
      const opt = document.createElement('option');
      opt.value = String(idx);
      opt.textContent = p.name || `预设 ${idx + 1}`;
      presetSelect.appendChild(opt);
    });
  };
  const getSnapshot = () => {
    const toList = (arr) => arr.map((c) => c.value).filter(Boolean);
    return {
      name: '',
      states: toList(stateChecks.filter((c) => c.checked)),
      comments: toList(commentChecks.filter((c) => c.checked)),
      labels: toList(issueLabelChecks.filter((c) => c.checked)),
      prTypes: toList(prTypeChecks.filter((c) => c.checked)),
      targets: toList(targetChecks.filter((c) => c.checked)),
      users: userChecks.filter((c) => c.checked).map((c) => c.value),
      groups: groupChecks.filter((c) => c.checked).map((c) => c.value),
      hideEmpty: filterHideEmptyUsers?.checked ?? true,
      hideClean: filterHideClean?.checked ?? false,
      onlyUnresolved: filterUnresolved?.checked ?? false,
      hideReplies: filterHideReplies?.checked ?? false,
      onlyResolved: filterResolvedOnly?.checked ?? false,
      commentKeyword: filterCommentKeyword?.value || '',
      commentExclude: filterCommentExclude?.value || '',
      dateField: filterDateField?.value || 'created',
      dateStart: filterDateStart?.value || '',
      dateEnd: filterDateEnd?.value || '',
      sortKey: getSortKey(),
    };
  };
  const applySnapshot = (snap) => {
    if (!snap) return;
    stateChecks.forEach((c) => (c.checked = snap.states.includes(c.value)));
    commentChecks.forEach((c) => (c.checked = snap.comments.includes(c.value)));
    issueLabelChecks.forEach((c) => (c.checked = snap.labels.includes(c.value)));
    prTypeChecks.forEach((c) => (c.checked = snap.prTypes ? snap.prTypes.includes(c.value) : true));
    targetChecks.forEach((c) => (c.checked = snap.targets ? snap.targets.includes(c.value) : true));
    userChecks.forEach((c) => (c.checked = snap.users.includes(c.value)));
    groupChecks.forEach((c) => (c.checked = snap.groups.includes(c.value)));
    if (filterHideEmptyUsers) filterHideEmptyUsers.checked = !!snap.hideEmpty;
    if (filterHideClean) filterHideClean.checked = !!snap.hideClean;
    if (filterUnresolved) filterUnresolved.checked = !!snap.onlyUnresolved;
    if (filterHideReplies) filterHideReplies.checked = !!snap.hideReplies;
    if (filterResolvedOnly) filterResolvedOnly.checked = !!snap.onlyResolved;
    if (filterCommentKeyword) filterCommentKeyword.value = snap.commentKeyword || '';
    if (filterCommentExclude) filterCommentExclude.value = snap.commentExclude || '';
    if (filterDateField && snap.dateField) filterDateField.value = snap.dateField;
    if (filterDateStart) filterDateStart.value = snap.dateStart || '';
    if (filterDateEnd) filterDateEnd.value = snap.dateEnd || '';
    if (sortSelect && snap.sortKey) sortSelect.value = snap.sortKey;
    wrappedApply();
  };
  if (presetSaveBtn) {
    presetSaveBtn.addEventListener('click', () => {
      const name = prompt('请输入预设名称');
      if (!name) return;
      const list = loadPresets();
      const snap = getSnapshot();
      snap.name = name;
      list.unshift(snap);
      savePresets(list.slice(0, 10)); // 最多保存 10 个
      syncPresetOptions();
      alert('已保存预设');
    });
  }
  if (presetApplyBtn && presetSelect) {
    presetApplyBtn.addEventListener('click', () => {
      const idx = parseInt(presetSelect.value || '-1', 10);
      if (Number.isNaN(idx) || idx < 0) return;
      const list = loadPresets();
      const snap = list[idx];
      applySnapshot(snap);
    });
  }
  syncPresetOptions();

  // 日期快捷
  document.querySelectorAll('.date-quick-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const range = parseInt(btn.dataset.range || '0', 10);
      if (!filterDateStart || !filterDateEnd) return;
      if (range === 0) {
        filterDateStart.value = '';
        filterDateEnd.value = '';
      } else {
        const end = new Date();
        const start = new Date();
        start.setDate(end.getDate() - range + 1);
        const pad = (n) => String(n).padStart(2, '0');
        const fmt = (dt) =>
          `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
        filterDateStart.value = fmt(start);
        filterDateEnd.value = fmt(end);
      }
      wrappedApply();
    });
  });

  const refreshUserToggleText = (selectedUsers) => {
    if (!userToggle) return;
    if (!userChecks.length) {
      userToggle.textContent = "用户：无";
      return;
    }
    if (!selectedUsers || selectedUsers.size === userChecks.length) {
      userToggle.textContent = "用户：全部";
    } else if (selectedUsers.size === 0) {
      userToggle.textContent = "用户：无";
    } else if (selectedUsers.size <= 3) {
      userToggle.textContent = `用户：${Array.from(selectedUsers).join(", ")}`;
    } else {
      userToggle.textContent = `用户：${selectedUsers.size} 个已选`;
    }
  };

  const refreshGroupToggleText = (selectedGroups) => {
    if (!groupToggle) return;
    if (!groupChecks.length) {
      groupToggle.textContent = "用户组：无";
      return;
    }
    if (!selectedGroups || selectedGroups.size === groupChecks.length) {
      groupToggle.textContent = "用户组：全部";
    } else if (selectedGroups.size === 0) {
      groupToggle.textContent = "用户组：无";
    } else if (selectedGroups.size <= 2) {
      groupToggle.textContent = `用户组：${Array.from(selectedGroups).join(", ")}`;
    } else {
      groupToggle.textContent = `用户组：${selectedGroups.size} 个已选`;
    }
  };

  const applyFilters = () => {
    const keyword = (filterCommentKeyword?.value || '').trim().toLowerCase();
    const hasKeyword = keyword.length > 0;
    const hideReplies = filterHideReplies?.checked;
    const onlyUnresolved = filterUnresolved.checked;
    const hideClean = filterHideClean.checked;
    const hideEmptyUsers = filterHideEmptyUsers?.checked;
    const sortKey = getSortKey();
    const selectedStates = getSelectedStates();
    const selectedComments = getSelectedCommentKinds();
    const selectedIssueLabels = getSelectedIssueLabels();
    const selectedPrTypes = getSelectedPrTypes();
    const selectedTargets = getSelectedTargets();
    const selectedUsers = getSelectedUsers();
    const selectedGroups = getSelectedGroups();
    const dateField = filterDateField?.value || 'created';
    const selectedGroupUsers = new Set();
    if (selectedGroups) {
      selectedGroups.forEach((name) => {
        const arr = groupMembers[name] || [];
        arr.forEach((u) => selectedGroupUsers.add(u));
      });
    }
    refreshUserToggleText(selectedUsers);
    refreshGroupToggleText(selectedGroups);

    document.querySelectorAll('.pr-card').forEach((card) => {
      const reviewWrapper = card.querySelector('[data-review-wrapper]');
      const reviewItems = reviewWrapper
        ? Array.from(reviewWrapper.querySelectorAll('.review-item'))
        : [];

      const unresolvedCount =
        parseInt(card.dataset.unresolvedCount || '0', 10) || 0;
      const resolvedCount =
        parseInt(card.dataset.resolvedCount || '0', 10) || 0;
      const totalComments =
        parseInt(card.dataset.totalComments || '0', 10) || 0;
      const hasUnresolved = unresolvedCount > 0;
      card.dataset.hasUnresolved = hasUnresolved ? '1' : '0';

      const state = (card.dataset.state || '').toLowerCase();
      const stateAllowed = selectedStates.has(state);
      const hasReview = totalComments > 0;
      const hasResolved = resolvedCount > 0;
      const commentTags = [];
      if (hasUnresolved) commentTags.push('unresolved');
      if (hasResolved) commentTags.push('resolved');
      if (!hasReview) commentTags.push('none');
      const commentAllowed = commentTags.some((t) =>
        selectedComments.has(t)
      );
      const dateStr =
        dateField === 'updated' ? card.dataset.updated || '' : card.dataset.created || '';
      const createdTs = Date.parse(dateStr);
      let dateAllowed = true;
      if (filterDateStart && filterDateStart.value) {
        const from = Date.parse(filterDateStart.value);
        if (!Number.isNaN(from) && !Number.isNaN(createdTs)) {
          dateAllowed = dateAllowed && createdTs >= from;
        }
      }
      if (filterDateEnd && filterDateEnd.value) {
        const to = Date.parse(filterDateEnd.value);
        if (!Number.isNaN(to) && !Number.isNaN(createdTs)) {
          // inclusive of end date day
          dateAllowed = dateAllowed && createdTs <= to + 24 * 60 * 60 * 1000;
        }
      }
      const issueLabelStr = card.dataset.issueLabels || '';
      const issueLabels = issueLabelStr ? issueLabelStr.split('||').filter(Boolean) : [];
      let issueAllowed = true;
      if (selectedIssueLabels.size) {
        issueAllowed = issueLabels.some((lab) => selectedIssueLabels.has(lab));
      }
      const prType = card.dataset.prType || '';
      const typeAllowed = selectedPrTypes.size
        ? selectedPrTypes.has(prType)
        : true;
      const target = card.dataset.target || '';
      const targetAllowed = selectedTargets.size
        ? selectedTargets.has(target)
        : true;
  const matchWholeWord = (text, kw) => {
    if (!kw) return true;
    const lowerText = (text || '').toLowerCase();
    const lowerKw = kw.toLowerCase();
    return lowerText.includes(lowerKw);
  };
  const keywordMatchedReviews = [];
  const replyKeywordParents = new Set();
  const replyExcludeParents = new Set();
  const parentResolvedMap = new Map();
  const visibleParents = new Set();
  reviewItems.forEach((it) => {
    const isReply = it.dataset.isReply === '1';
        const user = (it.dataset.user || '').trim();
    const bodyNode =
      it.querySelector('.review-body') || it.querySelector('.review-body-content');
    const bodyText = (bodyNode ? bodyNode.textContent : it.textContent) || '';
    const excludeKw = (filterCommentExclude?.value || '').trim();
    const hasExclude = excludeKw.length > 0;
    const excludeHit = isReply && hasExclude && matchWholeWord(bodyText, excludeKw);
    const onlyResolved = filterResolvedOnly?.checked;
    const matchesKeyword = isReply
      ? matchWholeWord(bodyText, keyword)
      : !hasKeyword;
    const isResolved = it.dataset.resolved === 'true';
    const commentId = it.dataset.commentId;
    if (!isReply && commentId) {
      parentResolvedMap.set(commentId, isResolved);
    }
    const baseVisible =
      matchesKeyword &&
      !excludeHit &&
      (!onlyUnresolved || !isResolved) &&
      (!onlyResolved || isResolved);
        const visible = baseVisible && !(hideReplies && isReply);
        it.style.display = visible ? '' : 'none';
        it.dataset._visible = visible ? '1' : '0';
        if (!isReply && visible && commentId) {
          visibleParents.add(commentId);
        }
        if (matchesKeyword) {
          keywordMatchedReviews.push(it);
        }
        if (matchesKeyword && isReply && it.dataset.parentId) {
          replyKeywordParents.add(it.dataset.parentId);
        }
        if (excludeHit && isReply && it.dataset.parentId) {
          replyExcludeParents.add(it.dataset.parentId);
        }
      });
      if (replyKeywordParents.size) {
        reviewItems.forEach((it) => {
          if (it.dataset.isReply === '1') return;
          const cid = it.dataset.commentId;
          if (cid && replyKeywordParents.has(cid)) {
            if (onlyUnresolved && parentResolvedMap.get(cid) === true) return;
            it.style.display = '';
            it.dataset._visible = '1';
            visibleParents.add(cid);
          }
        });
      }
      if (replyExcludeParents.size) {
        reviewItems.forEach((it) => {
          const isReply = it.dataset.isReply === '1';
          const cid = it.dataset.commentId;
          const pid = it.dataset.parentId;
          if (!isReply && cid && replyExcludeParents.has(cid)) {
            it.style.display = 'none';
            it.dataset._visible = '0';
          }
          if (isReply && pid && replyExcludeParents.has(pid)) {
            it.style.display = 'none';
            it.dataset._visible = '0';
          }
        });
      }
      // hide replies whose parent is not visible
      reviewItems.forEach((it) => {
        if (it.dataset.isReply !== '1') return;
        const pid = it.dataset.parentId;
        if (hideReplies) return;
        if (pid && !visibleParents.has(pid)) {
          it.style.display = 'none';
          it.dataset._visible = '0';
        }
      });
      const keywordAllowed = !hasKeyword || keywordMatchedReviews.length > 0;

      const shouldHidePr =
        !stateAllowed ||
        !commentAllowed ||
        !dateAllowed ||
        !issueAllowed ||
        !typeAllowed ||
        !targetAllowed ||
        !keywordAllowed ||
        (hideClean && state !== 'open' && !hasUnresolved);
      card.style.display = shouldHidePr ? 'none' : '';

      const reviewerGroups = reviewWrapper
        ? Array.from(reviewWrapper.querySelectorAll('.reviewer-group'))
        : [];
      reviewerGroups.forEach((group) => {
        const items = Array.from(group.querySelectorAll('.review-item'));
        const visible = items.some((it) => it.style.display !== 'none');
        group.style.display = visible ? '' : 'none';
      });

      const emptyUnresolved = reviewWrapper
        ? reviewWrapper.querySelector('[data-empty-unresolved]')
        : null;
      const emptyAll = reviewWrapper
        ? reviewWrapper.querySelector('[data-empty-all]')
        : null;
      const visibleReviews = reviewItems.filter(
        (it) => it.style.display !== 'none'
      );
      const hasVisibleReviews = visibleReviews.length > 0;

      if (onlyUnresolved) {
        if (emptyUnresolved) {
          emptyUnresolved.style.display = hasVisibleReviews ? 'none' : 'block';
        }
        if (emptyAll) {
          emptyAll.style.display = 'none';
        }
      } else {
        if (emptyUnresolved) {
          emptyUnresolved.style.display = 'none';
        }
        if (emptyAll) {
          const defaultText =
            emptyAll.dataset.defaultText || emptyAll.textContent || '';
          if (!emptyAll.dataset.defaultText) {
            emptyAll.dataset.defaultText = defaultText;
          }
          if (!hasVisibleReviews) {
            emptyAll.textContent =
              hasKeyword && reviewItems.length > 0
                ? '无匹配该关键字的检视意见'
                : defaultText || '无检视意见';
            emptyAll.style.display = 'block';
          } else {
            emptyAll.textContent = defaultText;
            emptyAll.style.display = 'none';
          }
        }
      }
    });

    document.querySelectorAll('[data-user-block]').forEach((userBlock) => {
      const username = (userBlock.dataset.username || '').trim();
      const userAllowed =
        (!selectedUsers && !selectedGroups) ||
        (selectedUsers && selectedUsers.has(username)) ||
        (selectedGroups && selectedGroupUsers.has(username)) ||
        !username;

      const cards = Array.from(userBlock.querySelectorAll('.pr-card'));
      const visibleCards = userAllowed
        ? cards.filter((c) => c.style.display !== 'none')
        : [];
      // 排序：在当前用户块内重新排列
      const sortedCards = [...visibleCards].sort((a, b) => {
        const parseDate = (v) => {
          const t = Date.parse(v);
          return Number.isNaN(t) ? 0 : t;
        };
        if (sortKey === 'updated') {
          return parseDate(b.dataset.updated) - parseDate(a.dataset.updated);
        }
        if (sortKey === 'unresolved') {
          const ua = parseInt(a.dataset.unresolvedCount || '0', 10) || 0;
          const ub = parseInt(b.dataset.unresolvedCount || '0', 10) || 0;
          if (ub !== ua) return ub - ua;
          return parseDate(b.dataset.created) - parseDate(a.dataset.created);
        }
        // 默认：创建时间
        return parseDate(b.dataset.created) - parseDate(a.dataset.created);
      });
      const grid = userBlock.querySelector('.pr-grid');
      if (grid && sortedCards.length) {
        sortedCards.forEach((card) => grid.appendChild(card));
      }
      const meta = userBlock.querySelector('[data-user-count]');
      if (meta) {
        meta.textContent = `共 ${visibleCards.length} 个 PR（当前筛选）`;
      }
      // 按开关控制空用户是否隐藏；不在筛选范围内的用户始终隐藏
      const shouldHideUser =
        !userAllowed || (hideEmptyUsers && visibleCards.length === 0);
      userBlock.style.display = shouldHideUser ? 'none' : '';
    });

    document.querySelectorAll('[data-repo-block]').forEach((repoBlock) => {
      const cards = Array.from(repoBlock.querySelectorAll('.pr-card'));
      const visibleCards = cards.filter((c) => {
        const userBlock = c.closest('[data-user-block]');
        const userHidden =
          userBlock && userBlock.style.display && userBlock.style.display !== '';
        return c.style.display !== 'none' && !userHidden;
      });
      const meta = repoBlock.querySelector('[data-repo-count]');
      if (meta) {
        meta.textContent = `共 ${visibleCards.length} 个 PR（当前筛选）`;
      }
    });
  };

  const updateSummary = () => {
    if (!filterSummary) return;
    const fmtDate = (val) => {
      if (!val) return '';
      const dt = new Date(val);
      if (!Number.isNaN(dt.getTime())) {
        const pad = (n) => String(n).padStart(2, '0');
        return `${dt.getFullYear()}/${pad(dt.getMonth() + 1)}/${pad(dt.getDate())}`;
      }
      return val.replace(/-/g, '/');
    };
    const stateLabels = { open: 'open', merged: 'merged' };
    const commentLabels = {
      unresolved: '未解决',
      resolved: '已解决',
      none: '无检视',
    };
    const states = Array.from(getSelectedStates());
    const comments = Array.from(getSelectedCommentKinds());
    const labels = Array.from(getSelectedIssueLabels());
    const prTypes = Array.from(getSelectedPrTypes());
    const targets = Array.from(getSelectedTargets());
    const keyword = (filterCommentKeyword?.value || '').trim();
    const excludeKeyword = (filterCommentExclude?.value || '').trim();
    const hideRepliesText = filterHideReplies?.checked ? "不含回复" : "含回复";
    const dateFieldText = (filterDateField?.value || 'created') === 'updated' ? '更新时间' : '创建时间';
    const displayResolvedText = filterResolvedOnly?.checked
      ? "仅已解决"
      : filterUnresolved?.checked
      ? "仅未解决"
      : "全部";
    const sortTextMap = {
      created: '创建时间 新→旧',
      updated: '更新时间 新→旧',
      unresolved: '未解决数 多→少',
    };
    const statesText = states.length
      ? states.map((s) => stateLabels[s] || s).join(", ")
      : "全部";
    const commentsText = comments.length
      ? comments.map((s) => commentLabels[s] || s).join(", ")
      : "全部";
    const labelText = labels.length ? labels.join(", ") : "全部";
    const prTypeText = prTypes.length ? prTypes.join(", ") : "全部";
    const targetText = targets.length ? targets.join(", ") : "全部";
    const keywordText = keyword || "不限";
    const excludeText = excludeKeyword || "不限";
    const dateFrom = fmtDate(filterDateStart?.value || "");
    const dateTo = fmtDate(filterDateEnd?.value || "");
    let datePart = "全部时间";
    if (dateFrom || dateTo) {
      datePart = `${dateFrom || '不限'} ~ ${dateTo || '不限'}`;
    }
    const hideEmpty = filterHideEmptyUsers?.checked ? "隐藏空用户" : "显示空用户";
    const sortText = sortTextMap[getSortKey()] || '创建时间 新→旧';
    filterSummary.textContent = `当前筛选：状态(${statesText}) · 检视(${commentsText}) · 评论显示(${displayResolvedText}) · 回复(${hideRepliesText}) · 回复包含(${keywordText}) · 回复不包含(${excludeText}) · 标签(${labelText}) · 类型(${prTypeText}) · 目标(${targetText}) · 日期(${datePart}, ${dateFieldText}) · ${hideEmpty} · 排序(${sortText})`;
  };

  const getReviewKeyword = () => ((reviewUserKeyword?.value || '').trim().toLowerCase());
  const getReviewSortKey = () => (reviewSortSelect ? (reviewSortSelect.value || 'total') : 'total');

  const reviewSortCompare = (a, b, sortKey) => {
    if (sortKey === 'name') {
      return (a.user || '').localeCompare(b.user || '');
    }
    if (sortKey === 'resolved') {
      if ((b.resolved || 0) !== (a.resolved || 0)) return (b.resolved || 0) - (a.resolved || 0);
      if ((b.unresolved || 0) !== (a.unresolved || 0)) return (b.unresolved || 0) - (a.unresolved || 0);
      return (a.user || '').localeCompare(b.user || '');
    }
    if (sortKey === 'unresolved') {
      if ((b.unresolved || 0) !== (a.unresolved || 0)) return (b.unresolved || 0) - (a.unresolved || 0);
      if ((b.resolved || 0) !== (a.resolved || 0)) return (b.resolved || 0) - (a.resolved || 0);
      return (a.user || '').localeCompare(b.user || '');
    }
    // default: total
    if ((b.total || 0) !== (a.total || 0)) return (b.total || 0) - (a.total || 0);
    if ((b.unresolved || 0) !== (a.unresolved || 0)) return (b.unresolved || 0) - (a.unresolved || 0);
    return (a.user || '').localeCompare(b.user || '');
  };

  const parseCodeStats = (raw) => {
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  };

  const sumExtStats = (stats, exts) => {
    let add = 0;
    let del = 0;
    if (!stats || !exts) return { add, del };
    exts.forEach((ext) => {
      const bucket = stats[ext];
      if (!bucket) return;
      add += parseInt(bucket.additions || 0, 10) || 0;
      del += parseInt(bucket.deletions || 0, 10) || 0;
    });
    return { add, del };
  };

  const summarizeExtStats = (stats, max = 3) => {
    if (!stats) return '';
    const list = Object.entries(stats).map(([ext, val]) => ({
      ext,
      add: parseInt(val.additions || 0, 10) || 0,
      del: parseInt(val.deletions || 0, 10) || 0,
    }));
    list.sort((a, b) => (b.add + b.del) - (a.add + a.del));
    const parts = list.slice(0, max).map((it) => `${it.ext} +${it.add}/-${it.del}`);
    if (!parts.length) return '';
    return parts.join(' · ') + (list.length > max ? ' 等' : '');
  };

  const buildFetchRange = () => {
    const field = fetchDateField?.value === 'updated' ? 'updated' : 'created';
    const start = fetchDateStart?.value || '';
    const end = fetchDateEnd?.value || '';
    const startRaw = start ? Date.parse(start) : null;
    const startMs = Number.isNaN(startRaw) ? null : startRaw;
    const endRaw = end ? Date.parse(end) : null;
    let endMs = Number.isNaN(endRaw) ? null : endRaw;
    if (endMs !== null) {
      endMs += 24 * 60 * 60 * 1000;
    }
    const startIso = start ? new Date(`${start}T00:00:00`).toISOString() : '';
    const endIso = end ? new Date(`${end}T23:59:59`).toISOString() : '';
    return { field, start, end, startMs, endMs, startIso, endIso };
  };
  const isPrInRange = (pr, range) => {
    if (!range) return true;
    const ts = Date.parse(
      range.field === 'updated'
        ? (pr.updated_at || pr.created_at || '')
        : (pr.created_at || '')
    );
    if (Number.isNaN(ts)) return true;
    if (range.startMs && ts < range.startMs) return false;
    if (range.endMs && ts > range.endMs) return false;
    return true;
  };

  const escapeHtml = (s) => {
    return (s ?? '')
      .toString()
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  };

  const renderCommentBody = (body) => {
    if (!body) return '';
    const lines = body.split(/\\r?\\n/);
    let inCode = false;
    let codeLines = [];
    const parts = [];
    const renderTextLine = (line) => {
      const segments = line.split('`');
      const out = [];
      segments.forEach((seg, idx) => {
        if (idx % 2 === 0) {
          out.push(escapeHtml(seg));
        } else {
          out.push(`<code class='review-code-inline'>${escapeHtml(seg)}</code>`);
        }
      });
      return out.join('');
    };
    lines.forEach((line) => {
      if (line.startsWith('```')) {
        if (!inCode) {
          inCode = true;
          codeLines = [];
        } else {
          const codeHtml =
            "<pre class='review-code-block'><code>" +
            escapeHtml(codeLines.join('\\n')) +
            "</code></pre>";
          parts.push(codeHtml);
          inCode = false;
          codeLines = [];
        }
        return;
      }
      if (inCode) {
        codeLines.push(line);
      } else {
        parts.push(renderTextLine(line) + "<br/>");
      }
    });
    if (inCode && codeLines.length) {
      codeLines.forEach((line) => {
        parts.push(renderTextLine(line) + "<br/>");
      });
    }
    return parts.join('');
  };

  const isWipTitle = (title) => {
    if (!title) return false;
    const t = title.trim().toLowerCase();
    return t.startsWith('wip') || t.startsWith('[wip]') || t.startsWith('wip:');
  };

  const inferPrType = (title) => {
    if (!title) return '';
    const m = title.trim().match(/^([A-Za-z0-9_-]+)\\s*:/);
    if (m) {
      const prefix = m[1].toLowerCase();
      return ALLOWED_PR_TYPES.has(prefix) ? prefix : '';
    }
    return '';
  };

  const extFromFilename = (name) => {
    const parts = (name || '').split('/');
    const base = parts.pop() || '';
    if (!base) return '(no_ext)';
    const dot = base.lastIndexOf('.');
    if (dot > 0) return base.slice(dot).toLowerCase();
    if (base.startsWith('.') && base.length > 1) return base.toLowerCase();
    return '(no_ext)';
  };

  const normalizeIssueUrl = (url) => {
    if (!url) return '';
    return url.replace('api.gitcode', 'gitcode').replace('api/v5/repos/', '');
  };

  const inferResolved = (comment) => {
    if (comment && Object.prototype.hasOwnProperty.call(comment, 'resolved')) {
      const val = comment.resolved;
      if (typeof val === 'boolean') return val;
      if (typeof val === 'string') {
        const v = val.toLowerCase();
        if (['true', '1', 'yes', 'resolved'].includes(v)) return true;
        if (['false', '0', 'no', 'unresolved'].includes(v)) return false;
      }
    }
    const status = comment?.status;
    if (typeof status === 'string') {
      const v = status.toLowerCase();
      if (['resolved', 'done'].includes(v)) return true;
      if (['unresolved', 'open', 'todo'].includes(v)) return false;
    }
    return null;
  };

  const collectMetaFromData = (data) => {
    const issueLabels = new Set();
    const prTypes = new Set();
    const targets = new Set();
    Object.values(data || {}).forEach((users) => {
      Object.values(users || {}).forEach((prs) => {
        (prs || []).forEach((pr) => {
          const prType = inferPrType(pr.title || '');
          if (prType) prTypes.add(prType);
          const tgt = (pr.target_branch || '').trim();
          if (tgt) targets.add(tgt);
          (pr.issues || []).forEach((iss) => {
            (iss.labels || []).forEach((lab) => {
              if (!lab) return;
              issueLabels.add(String(lab));
            });
          });
        });
      });
    });
    return {
      issueLabels: Array.from(issueLabels),
      prTypes: Array.from(prTypes),
      targets: Array.from(targets),
    };
  };

  const renderDynamicFilters = (meta) => {
    const labels = meta?.issueLabels || [];
    const prTypes = meta?.prTypes || [];
    const targets = meta?.targets || [];
    const prevLabels = new Set(issueLabelChecks.filter((c) => c.checked).map((c) => c.value));
    const prevTypes = new Set(prTypeChecks.filter((c) => c.checked).map((c) => c.value));
    const prevTargets = new Set(targetChecks.filter((c) => c.checked).map((c) => c.value));

    const buildList = (listEl, values, cls, prev, defaultChecked = false) => {
      if (!listEl) return [];
      listEl.innerHTML = '';
      const nodes = [];
      values.forEach((val) => {
        const label = document.createElement('label');
        label.className = 'filter-label';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.className = cls;
        input.value = val;
        const shouldCheck = prev.size ? prev.has(val) : defaultChecked;
        input.checked = shouldCheck;
        label.appendChild(input);
        label.append(` ${val}`);
        listEl.appendChild(label);
        nodes.push(input);
      });
      return nodes;
    };

    issueLabelChecks = buildList(issueList, labels, 'filter-issue-label-checkbox', prevLabels, false);
    prTypeChecks = buildList(prTypeList, prTypes, 'filter-pr-type-checkbox', prevTypes, false);
    targetChecks = buildList(targetList, targets, 'filter-target-checkbox', prevTargets, true);

    if (issueGroup) issueGroup.style.display = labels.length ? '' : 'none';
    if (prTypeGroup) prTypeGroup.style.display = prTypes.length ? '' : 'none';
    if (targetGroup) targetGroup.style.display = targets.length ? '' : 'none';

    issueLabelChecks.forEach((c) => c.addEventListener('change', wrappedApply));
    prTypeChecks.forEach((c) => c.addEventListener('change', wrappedApply));
    targetChecks.forEach((c) => c.addEventListener('change', wrappedApply));
  };

  const buildCardView = (data) => {
    if (!cardView) return;
    const repoNames = Object.keys(data || {});
    if (!repoNames.length) {
      cardView.innerHTML = "<p class='empty-text'>没有任何符合条件的 PR。</p>";
      return;
    }
    const parts = [];
    const parseTs = (ts) => {
      if (!ts) return 0;
      const t = ts.replace('Z', '+00:00');
      const ms = Date.parse(t);
      return Number.isNaN(ms) ? 0 : ms;
    };
    const prSortKey = (pr) => {
      const rankMap = { open: 0, merged: 1 };
      const rank = rankMap[(pr.state || '').toLowerCase()] ?? 2;
      return [rank, -parseTs(pr.created_at || ''), -(parseInt(pr.number || 0, 10) || 0)];
    };
    const prSortCompare = (a, b) => {
      const ka = prSortKey(a);
      const kb = prSortKey(b);
      for (let i = 0; i < ka.length; i += 1) {
        if (ka[i] !== kb[i]) return ka[i] - kb[i];
      }
      return 0;
    };

    repoNames.forEach((repoName) => {
      const users = data[repoName] || {};
      const totalPrs = Object.values(users).reduce((acc, prs) => acc + (prs ? prs.length : 0), 0);
      parts.push("<details class='repo-block' open data-repo-block>");
      parts.push("<summary>");
      parts.push(`<div class='repo-title'>仓库：${escapeHtml(repoName)}<span class='repo-meta' data-repo-count>共 ${totalPrs} 个 PR（页面可再筛选）</span></div>`);
      parts.push("<div class='repo-chevron'>▶</div>");
      parts.push("</summary>");
      parts.push("<div class='repo-content'>");

      Object.entries(users).forEach(([username, prs]) => {
        const list = Array.isArray(prs) ? prs : [];
        if (!list.length) return;
        const sorted = [...list].sort(prSortCompare);
        parts.push(`<details class='user-block' open data-user-block data-username='${escapeHtml(username)}'>`);
        parts.push("<summary>");
        parts.push(`<div class='user-title'>用户：${escapeHtml(username)}<span class='user-meta' data-user-count>共 ${list.length} 个 PR</span></div>`);
        parts.push("<div class='user-chevron'>▶</div>");
        parts.push("</summary>");
        parts.push("<div class='user-content'>");
        parts.push("<div class='pr-grid'>");

        sorted.forEach((pr) => {
          const allComments = pr.comments || [];
          const parentComments = allComments.filter((cm) => !cm.is_reply);
          const unresolvedCount = parentComments.filter((cm) => cm.resolved === false).length;
          const resolvedCount = parentComments.filter((cm) => cm.resolved === true).length;
          const issueLabels = [];
          (pr.issues || []).forEach((iss) => {
            (iss.labels || []).forEach((lab) => {
              if (!lab) return;
              if (!issueLabels.includes(lab)) issueLabels.push(lab);
            });
          });
          const prType = inferPrType(pr.title || '');
          const codeKnown =
            pr.additions !== null &&
            pr.additions !== undefined &&
            pr.deletions !== null &&
            pr.deletions !== undefined &&
            pr.changed_files !== null &&
            pr.changed_files !== undefined;
          const extSummary = pr.file_stats ? summarizeExtStats(pr.file_stats) : '';
          const codeText = codeKnown
            ? `代码变更：+${pr.additions} / -${pr.deletions} · 文件 ${pr.changed_files}${extSummary ? ` · 后缀：${extSummary}` : ''}`
            : '代码变更：未知';

          let badgeCls = 'badge-warn';
          let badgeText = '无检视意见';
          if (unresolvedCount > 0) {
            badgeCls = 'badge-danger';
            badgeText = `${unresolvedCount} 未解决`;
          } else if (allComments.length) {
            badgeCls = 'badge-ok';
            badgeText = '无未解决检视意见';
          }

          const stateLower = (pr.state || '').toLowerCase();
          parts.push(
            "<div class='pr-card'" +
            ` data-state='${escapeHtml(stateLower)}'` +
            ` data-has-unresolved='${unresolvedCount > 0 ? 1 : 0}'` +
            ` data-total-comments='${allComments.length}'` +
            ` data-unresolved-count='${unresolvedCount}'` +
            ` data-resolved-count='${resolvedCount}'` +
            ` data-code-known='${codeKnown ? '1' : '0'}'` +
            ` data-additions='${codeKnown ? pr.additions : ''}'` +
            ` data-deletions='${codeKnown ? pr.deletions : ''}'` +
            ` data-changed-files='${codeKnown ? pr.changed_files : ''}'` +
            ` data-code-stats='${escapeHtml(JSON.stringify(pr.file_stats || {}))}'` +
            ` data-created='${escapeHtml(pr.created_at || '')}'` +
            ` data-updated='${escapeHtml(pr.updated_at || '')}'` +
            ` data-issue-labels='${escapeHtml(issueLabels.join('||'))}'` +
            ` data-pr-number='${pr.number}'` +
            ` data-title='${escapeHtml(pr.title || '')}'` +
            ` data-url='${escapeHtml(pr.html_url || '')}'` +
            ` data-repo='${escapeHtml(repoName)}'` +
            ` data-username='${escapeHtml(username)}'` +
            ` data-source='${escapeHtml(pr.source_branch || '')}'` +
            ` data-target='${escapeHtml(pr.target_branch || '')}'` +
            ` data-pr-type='${escapeHtml(prType)}'>`
          );

          parts.push("<div class='pr-header'>");
          const titleText = `#${pr.number} ${pr.title || ''}`;
          const titleHtml = pr.html_url
            ? `<a class='pr-link-inline' href='${escapeHtml(pr.html_url)}' target='_blank' rel='noopener noreferrer'>${escapeHtml(titleText)}</a>`
            : escapeHtml(titleText);
          parts.push(`<div class='pr-title'>${titleHtml}</div>`);
          parts.push(`<span class='badge ${badgeCls}'>${escapeHtml(badgeText)}</span>`);
          parts.push("</div>");

          const stateCls =
            stateLower === 'open' ? 'state-open' : stateLower === 'merged' ? 'state-merged' : 'state-other';
          parts.push(`<div class='pr-meta'>状态：<span class='state-label ${stateCls}'>${escapeHtml(pr.state || '')}</span></div>`);

          let branchHtml = '';
          if (pr.target_branch) {
            const tb = pr.target_branch || '';
            const tbLower = tb.toLowerCase();
            let tgtCls = 'branch-target-other';
            if (['main', 'master', 'trunk'].includes(tbLower)) {
              tgtCls = 'branch-target-main';
            } else if (tbLower === 'dev' || tbLower === 'develop' || tbLower.includes('dev')) {
              tgtCls = 'branch-target-dev';
            } else if (tbLower.startsWith('release/') || tbLower.startsWith('release-')) {
              tgtCls = 'branch-target-release';
            } else if (tbLower.startsWith('hotfix/') || tbLower.startsWith('hotfix-')) {
              tgtCls = 'branch-target-hotfix';
            }
            const src = pr.source_branch || '';
            branchHtml =
              `${escapeHtml(src)} → ` +
              `<span class='branch-target-pill ${tgtCls}'>${escapeHtml(tb)}</span>`;
          } else if (pr.source_branch) {
            branchHtml = escapeHtml(pr.source_branch);
          }

          if (branchHtml) {
            parts.push(`<div class='pr-branch'>分支：${branchHtml}</div>`);
            let timesLine = `创建：${escapeHtml(pr.created_at || '')}`;
            if (pr.updated_at) timesLine += ` ｜ 更新：${escapeHtml(pr.updated_at)}`;
            parts.push(`<div class='pr-times'>${timesLine}</div>`);
            parts.push(`<div class='pr-code'>${escapeHtml(codeText)}</div>`);
          }

          parts.push("<div class='section-title'>关联 Issues</div>");
          if (!pr.issues || !pr.issues.length) {
            parts.push("<div class='empty-text'>无关联 Issue</div>");
          } else {
            pr.issues.forEach((iss) => {
              const labelsStr = iss.labels && iss.labels.length ? `（labels: ${iss.labels.join(', ')}）` : '';
              const issueText = `#${iss.number} [${iss.state}] ${iss.title}${labelsStr}`;
              const issueUrl = iss.url || iss.html_url || '';
              const issueHtml = issueUrl
                ? `<a class='issue-link' href='${escapeHtml(issueUrl)}' target='_blank' rel='noopener noreferrer'>${escapeHtml(issueText)}</a>`
                : escapeHtml(issueText);
              parts.push(`<div class='issue-item'>${issueHtml}</div>`);
            });
          }

          parts.push("<div class='section-title'>检视意见</div>");
          parts.push("<div class='reviews' data-review-wrapper>");
          if (!allComments.length) {
            parts.push("<div class='empty-text' data-empty-all>无需要 resolved 状态的检视意见</div>");
          } else {
            const parentCommentsAll = allComments.filter((cm) => !cm.is_reply);
            const grouped = new Map();
            parentCommentsAll.forEach((cm) => {
              const key = cm.user || '(unknown)';
              if (!grouped.has(key)) grouped.set(key, []);
              grouped.get(key).push(cm);
            });
            const parentIds = new Set(parentCommentsAll.map((cm) => cm.id));
            const repliesByParent = new Map();
            const orphanReplies = [];
            allComments.forEach((cm) => {
              if (!cm.is_reply) return;
              if (cm.parent_id != null && parentIds.has(cm.parent_id)) {
                if (!repliesByParent.has(cm.parent_id)) repliesByParent.set(cm.parent_id, []);
                repliesByParent.get(cm.parent_id).push(cm);
              } else {
                orphanReplies.push(cm);
              }
            });

            const renderComment = (cm, isReply = false) => {
              const isResolved = cm.resolved === true;
              const statusCls = isReply ? 'reply' : (isResolved ? 'resolved' : 'unresolved');
              const statusText = isReply ? '回复' : (isResolved ? '已解决' : '未解决');
              const resolvedAttr = isResolved ? 'true' : 'false';
              const isReplyAttr = isReply ? '1' : '0';
              const userAttr = escapeHtml(cm.user || '');
              const parentUserAttr = escapeHtml(cm.parent_user || '');
              const parentIdAttr = isReply && cm.parent_id != null ? ` data-parent-id='${cm.parent_id}'` : '';
              const commentIdAttr = ` data-comment-id='${cm.id}'`;
              let loc = '';
              if (cm.path) {
                loc = cm.path;
                if (cm.position != null) loc += `:${cm.position}`;
              }
              let headerLeft = statusText;
              if (loc) headerLeft += ` · ${loc}`;
              let html =
                `<div class='review-item ${statusCls}${isReply ? ' review-reply' : ''}'` +
                ` data-resolved='${resolvedAttr}' data-is-reply='${isReplyAttr}'` +
                ` data-user='${userAttr}' data-parent-user='${parentUserAttr}'${parentIdAttr}${commentIdAttr}>`;
              html += `<div class='review-header'><span>${escapeHtml(headerLeft)}</span></div>`;
              html += `<div class='review-meta'>创建：${escapeHtml(cm.created_at || '')} ｜ 更新：${escapeHtml(cm.updated_at || '')}</div>`;
              if (cm.body) {
                const bodyHtml = renderCommentBody(cm.body);
                const lineCount = (cm.body.match(/\\n/g) || []).length + 1;
                const isLong = lineCount >= 8 || cm.body.length >= 400;
                if (isLong) {
                  html += "<div class='review-body review-body-collapsible'><details>";
                  html += `<summary>展开完整评论（约 ${lineCount} 行）</summary>`;
                  html += `<div class='review-body-content'>${bodyHtml}</div>`;
                  html += "</details></div>";
                } else {
                  html += `<div class='review-body'>${bodyHtml}</div>`;
                }
              }
              html += "</div>";
              return html;
            };

            grouped.forEach((parentList, reviewer) => {
              const parentCount = parentList.length;
              const parentUnresolved = parentList.filter((cm) => cm.resolved === false).length;
              const parentResolved = parentList.filter((cm) => cm.resolved === true).length;
              parts.push("<details class='reviewer-group' open>");
              parts.push("<summary>");
              parts.push(
                `<div class='reviewer-group-title'>${escapeHtml(reviewer)}` +
                `<span>${parentCount} 条检视意见（未解决 ${parentUnresolved} · 已解决 ${parentResolved}）</span></div>`
              );
              parts.push("<div class='reviewer-chevron'>▶</div>");
              parts.push("</summary>");
              parts.push("<div class='reviewer-group-body'>");
              parentList.forEach((cm) => {
                parts.push(renderComment(cm, false));
                const child = repliesByParent.get(cm.id) || [];
                if (child.length) {
                  parts.push("<div class='review-replies'>");
                  child.forEach((rp) => parts.push(renderComment(rp, true)));
                  parts.push("</div>");
                }
              });
              parts.push("</div>");
              parts.push("</details>");
            });

            if (orphanReplies.length) {
              parts.push("<details class='reviewer-group' open>");
              parts.push("<summary>");
              parts.push("<div class='reviewer-group-title'>回复（无主）" +
                `<span>${orphanReplies.length} 条回复</span></div>`);
              parts.push("<div class='reviewer-chevron'>▶</div>");
              parts.push("</summary>");
              parts.push("<div class='reviewer-group-body'><div class='review-replies'>");
              orphanReplies.forEach((rp) => parts.push(renderComment(rp, true)));
              parts.push("</div></div></details>");
            }
          }
          parts.push("<div class='empty-text' data-empty-unresolved style='display:none'>无未解决的检视意见</div>");
          parts.push("</div>");

          parts.push("</div>");
        });

        parts.push("</div>");
        parts.push("</div>");
        parts.push("</details>");
      });

      parts.push("</div>");
      parts.push("</details>");
    });

    cardView.innerHTML = parts.join('');
  };

  const createLimiter = (limit) => {
    let active = 0;
    const queue = [];
    const runNext = () => {
      if (active >= limit || queue.length === 0) return;
      const item = queue.shift();
      active += 1;
      Promise.resolve()
        .then(item.fn)
        .then((res) => {
          active -= 1;
          item.resolve(res);
          runNext();
        })
        .catch((err) => {
          active -= 1;
          item.reject(err);
          runNext();
        });
    };
    return (fn) =>
      new Promise((resolve, reject) => {
        queue.push({ fn, resolve, reject });
        runNext();
      });
  };

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const updateInterval = (nextMs, reason) => {
    const clamped = Math.min(Math.max(nextMs, baseRequestIntervalMs), 8000);
    if (clamped === requestIntervalMs) return;
    requestIntervalMs = clamped;
    logInfo('请求间隔调整', `${requestIntervalMs}ms`, reason);
  };
  const noteSuccess = () => {
    successStreak += 1;
    if (successStreak >= 20 && requestIntervalMs > baseRequestIntervalMs) {
      updateInterval(requestIntervalMs - 200, '恢复');
      successStreak = 0;
    }
  };
  const noteRateLimit = (retryAfter) => {
    successStreak = 0;
    const boosted = Math.max(requestIntervalMs * 1.5, baseRequestIntervalMs + 400);
    updateInterval(boosted, '限流');
    const jitter = Math.floor(Math.random() * 300);
    rateLimitUntil = Math.max(rateLimitUntil, Date.now() + (retryAfter * 1000) + jitter);
  };
  let lastRequestAt = 0;
  let rateLimitUntil = 0;
  let throttleQueue = Promise.resolve();
  const throttleRequest = async () => {
    let release = null;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const prev = throttleQueue;
    throttleQueue = gate;
    await prev;
    try {
      const now = Date.now();
      if (rateLimitUntil && now < rateLimitUntil) {
        await wait(rateLimitUntil - now);
      }
      const now2 = Date.now();
      const waitMs = lastRequestAt + requestIntervalMs - now2;
      if (waitMs > 0) {
        await wait(waitMs);
      }
      lastRequestAt = Date.now();
    } finally {
      if (release) release();
    }
  };

  const parseErrorPayload = async (resp) => {
    const text = await resp.text();
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch (e) {
      payload = null;
    }
    return { text, payload };
  };

  const isRateLimitError = (status, payload, text) => {
    if (status === 429) return true;
    if (payload && (payload.error_code === 429 || payload.error_code === '429')) return true;
    if (payload && typeof payload.error_message === 'string' && payload.error_message.includes('429')) return true;
    if (text && text.includes('429')) return true;
    return false;
  };

  const fetchJson = async (path, token, params = {}) => {
    const url = new URL(API_BASE_URL + path);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === '') return;
      url.searchParams.set(k, String(v));
    });
    if (token) url.searchParams.set('access_token', token);
    const maxRetry = 4;
    for (let attempt = 0; attempt < maxRetry; attempt += 1) {
      await throttleRequest();
      let resp;
      try {
        resp = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
      } catch (err) {
        const msg = err?.message || String(err || '');
        logInfo('网络错误', path, msg);
        if (attempt === maxRetry - 1) {
          throw err;
        }
        await wait(1000 * (attempt + 1));
        continue;
      }
      if (!resp.ok) {
        const { text, payload } = await parseErrorPayload(resp);
        if (isRateLimitError(resp.status, payload, text)) {
          const retryAfterRaw = resp.headers.get('Retry-After') || '60';
          const retryAfter = Math.max(parseInt(retryAfterRaw, 10) || 60, 5);
          noteRateLimit(retryAfter);
          logInfo('触发限流', path, `等待 ${retryAfter}s`);
          if (typeof setRefreshStatus === 'function') {
            setRefreshStatus(`触发限流，等待 ${retryAfter}s...`, 'error');
          }
          if (attempt === maxRetry - 1) {
            throw new Error(`GitCode API 429: ${text.slice(0, 200)}`);
          }
          await wait(retryAfter * 1000);
          continue;
        }
        logInfo('API 失败', path, resp.status, text.slice(0, 120));
        if (attempt < maxRetry - 1 && resp.status >= 500) {
          await wait(800 * (attempt + 1));
          continue;
        }
        throw new Error(`GitCode API 请求失败: ${resp.status} ${text.slice(0, 200)}`);
      }
      noteSuccess();
      return resp.json();
    }
    return null;
  };

  const fetchPrsForUser = async (repoCfg, username, token, fetchMode, fetchRange, fetchStates) => {
    const allPrs = [];
    const seen = new Set();
    let states = normalizeFetchStates(Array.isArray(repoCfg.states) ? repoCfg.states : ['open']);
    if (states.includes('all') && states.length > 1) states = ['all'];
    const wanted = normalizeFetchStates(fetchStates);
    if (wanted.length) {
      if (states.includes('all')) {
        states = wanted;
      } else {
        states = states.filter((st) => wanted.includes(st));
      }
    }
    if (!states.length) {
      logInfo('抓取状态为空，跳过用户', `${repoCfg.owner}/${repoCfg.repo}`, username);
      return allPrs;
    }
    const perPage = Math.min(Math.max(parseInt(repoCfg.per_page || 30, 10) || 30, 1), 100);
    for (const state of states) {
      let page = 1;
      while (true) {
        const params = {
          state,
          author: username,
          page,
          per_page: perPage,
          only_count: 'false',
        };
        if (fetchMode === 'api' && fetchRange && (fetchRange.start || fetchRange.end)) {
          const afterKey = fetchRange.field === 'updated' ? 'updated_after' : 'created_after';
          const beforeKey = fetchRange.field === 'updated' ? 'updated_before' : 'created_before';
          if (fetchRange.startIso) params[afterKey] = fetchRange.startIso;
          if (fetchRange.endIso) params[beforeKey] = fetchRange.endIso;
          if (fetchRange.field === 'updated' && fetchRange.startIso) {
            params.since = fetchRange.startIso;
          }
        }
        const data = await fetchJson(
          `/repos/${repoCfg.owner}/${repoCfg.repo}/pulls`,
          token,
          params
        );
        if (!Array.isArray(data) || data.length === 0) break;
        data.forEach((pr) => {
          const num = parseInt(pr.number || 0, 10) || 0;
          const title = pr.title || '';
          if (pr.work_in_progress === true || pr.draft === true) return;
          if (isWipTitle(title)) return;
          if (seen.has(num)) return;
          seen.add(num);
          const head = pr.head || {};
          const base = pr.base || {};
          allPrs.push({
            number: num,
            title,
            state: pr.state || '',
            html_url: pr.html_url || '',
            created_at: pr.created_at || '',
            updated_at: pr.updated_at || '',
            merged_at: pr.merged_at || null,
            source_branch: head.ref || head.name || '',
            target_branch: base.ref || base.name || '',
            issues: [],
            comments: [],
            additions: null,
            deletions: null,
            changed_files: null,
            file_stats: {},
          });
        });
        if (data.length < perPage) break;
        page += 1;
        await new Promise((r) => setTimeout(r, 50));
      }
    }
    logInfo('PR 列表完成', `${repoCfg.owner}/${repoCfg.repo}`, username, allPrs.length);
    return allPrs;
  };

  const fetchIssuesForPr = async (repoCfg, prNumber, token) => {
    try {
      const data = await fetchJson(
        `/repos/${repoCfg.owner}/${repoCfg.repo}/pulls/${prNumber}/issues`,
        token,
        { page: 1, per_page: 100 }
      );
      if (!Array.isArray(data)) return [];
      return data.map((it) => ({
        number: String(it.number || ''),
        title: it.title || '',
        state: it.state || '',
        url: normalizeIssueUrl(it.url || it.html_url || ''),
        html_url: it.html_url || '',
        labels: Array.isArray(it.labels) ? it.labels.map((lab) => lab.name || '').filter(Boolean) : [],
      }));
    } catch (e) {
      return [];
    }
  };

  const fetchCommentsForPr = async (repoCfg, prNumber, token) => {
    const comments = [];
    let page = 1;
    while (true) {
      let data;
      try {
        data = await fetchJson(
          `/repos/${repoCfg.owner}/${repoCfg.repo}/pulls/${prNumber}/comments`,
          token,
          { page, per_page: 100, comment_type: 'diff_comment' }
        );
      } catch (e) {
        return comments;
      }
      if (!Array.isArray(data) || data.length === 0) break;
      const makeComment = (obj, opts = {}) => {
        const userObj = obj.user || {};
        const login = userObj.login || userObj.username || userObj.name || '';
        let pos = obj.position;
        if (pos == null) {
          const diffPos = obj.diff_position || {};
          pos = diffPos.start_new_line || diffPos.end_new_line;
        }
        let resolved = inferResolved(obj);
        if (resolved == null) resolved = false;
        return {
          id: parseInt(obj.id || 0, 10) || 0,
          user: login,
          body: obj.body || '',
          created_at: obj.created_at || '',
          updated_at: obj.updated_at || '',
          resolved,
          path: obj.path || opts.fallbackPath || null,
          position: pos != null ? pos : opts.fallbackPos || null,
          is_reply: !!opts.isReply,
          parent_user: opts.parentUser || null,
          parent_id: opts.parentId || null,
        };
      };
      data.forEach((c) => {
        const parent = makeComment(c, { isReply: false });
        comments.push(parent);
        const replies = Array.isArray(c.reply) ? c.reply : [];
        replies.forEach((r) => {
          comments.push(
            makeComment(r, {
              isReply: true,
              parentUser: parent.user,
              parentId: parent.id,
              fallbackPath: parent.path,
              fallbackPos: parent.position,
            })
          );
        });
      });
      if (data.length < 100) break;
      page += 1;
      await new Promise((r) => setTimeout(r, 50));
    }
    return comments;
  };

  const fetchFilesForPr = async (repoCfg, prNumber, token) => {
    let totalAdd = 0;
    let totalDel = 0;
    let totalFiles = 0;
    const stats = {};
    const seenItems = new Set();
    let page = 1;
    const perPage = 100;
    const maxPages = 10;
    while (true) {
      let data;
      try {
        data = await fetchJson(
          `/repos/${repoCfg.owner}/${repoCfg.repo}/pulls/${prNumber}/files`,
          token,
          { page, per_page: perPage }
        );
      } catch (e) {
        return { additions: null, deletions: null, changed_files: null, file_stats: {} };
      }
      if (!data) break;
      const items = Array.isArray(data) ? data : [data];
      let newItems = 0;
      items.forEach((it) => {
        const add = parseInt(it.additions || 0, 10) || 0;
        const del = parseInt(it.deletions || 0, 10) || 0;
        const name = it.filename || it.new_path || it.old_path || '';
        const key = `${name}|${add}|${del}`;
        if (seenItems.has(key)) return;
        seenItems.add(key);
        newItems += 1;
        const ext = extFromFilename(name);
        if (!CODE_STAT_SUFFIXES.has(ext)) return;
        totalAdd += add;
        totalDel += del;
        totalFiles += 1;
        const bucket = stats[ext] || { additions: 0, deletions: 0, files: 0 };
        bucket.additions += add;
        bucket.deletions += del;
        bucket.files += 1;
        stats[ext] = bucket;
      });
      if (!Array.isArray(data) || items.length < perPage || newItems === 0) {
        if (newItems === 0) {
          logInfo('文件分页重复，停止继续拉取', `${repoCfg.owner}/${repoCfg.repo}#${prNumber}`, `page=${page}`);
        }
        break;
      }
      page += 1;
      if (page > maxPages) break;
      await new Promise((r) => setTimeout(r, 50));
    }
    return { additions: totalAdd, deletions: totalDel, changed_files: totalFiles, file_stats: stats };
  };

  const fetchRepoUserData = async (repoCfg, username, token, detailLimiter, onProgress, fetchMode, fetchRange, fetchStates) => {
    const repoName = `${repoCfg.owner}/${repoCfg.repo}`;
    const t0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    logInfo('用户开始', repoName, username);
    const prsRaw = await fetchPrsForUser(repoCfg, username, token, fetchMode, fetchRange, fetchStates);
    const applyRange = fetchMode === 'details' || fetchMode === 'api';
    const prs = applyRange && fetchRange ? prsRaw.filter((pr) => isPrInRange(pr, fetchRange)) : prsRaw;
    if (applyRange && fetchRange) {
      logInfo('日期过滤 PR', repoName, username, `${prs.length}/${prsRaw.length}`);
    }
    const tasks = prs.map((pr) =>
      detailLimiter(async () => {
        if (onProgress) onProgress(`拉取 ${repoCfg.owner}/${repoCfg.repo} #${pr.number} 详情...`);
        const detailTasks = [
          fetchCommentsForPr(repoCfg, pr.number, token),
          fetchIssuesForPr(repoCfg, pr.number, token),
        ];
        if (CODE_STATS_ENABLED) {
          detailTasks.push(fetchFilesForPr(repoCfg, pr.number, token));
        }
        const results = await Promise.all(detailTasks);
        const comments = results[0] || [];
        const issues = results[1] || [];
        const fileStats = CODE_STATS_ENABLED
          ? (results[2] || { additions: null, deletions: null, changed_files: null, file_stats: {} })
          : { additions: null, deletions: null, changed_files: null, file_stats: {} };
        pr.comments = comments || [];
        pr.issues = issues || [];
        pr.additions = fileStats.additions;
        pr.deletions = fileStats.deletions;
        pr.changed_files = fileStats.changed_files;
        pr.file_stats = fileStats.file_stats || {};
        logInfo('PR 详情完成', repoName, `#${pr.number}`, {
          comments: pr.comments.length,
          issues: pr.issues.length,
          files: pr.changed_files,
          additions: pr.additions,
          deletions: pr.deletions,
        });
        return pr;
      })
    );
    const list = await Promise.all(tasks);
    const t1 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    logInfo('用户完成', repoName, username, { prs: list.length, ms: Math.round(t1 - t0) });
    return list;
  };

  const fetchAllData = async (token, onProgress, fetchMode, fetchRange, fetchStates) => {
    const data = {};
    const repos = Array.isArray(CLIENT_CONFIG.repos) ? CLIENT_CONFIG.repos : [];
    const users = getEffectiveUsers();
    logInfo('开始拉取', {
      repos: repos.length,
      users: users.length,
      codeStats: CODE_STATS_ENABLED,
      fetchMode,
      fetchStates,
      requestIntervalMs,
      repoConcurrency,
      detailConcurrency,
    });
    if (!users.length) {
      logInfo('未配置用户，跳过拉取');
      return data;
    }
    const repoLimiter = createLimiter(repoConcurrency);
    const detailLimiter = createLimiter(detailConcurrency);
    const tasks = [];
    repos.forEach((repoCfg) => {
      const repoName = `${repoCfg.owner}/${repoCfg.repo}`;
      data[repoName] = {};
      users.forEach((username) => {
        tasks.push(
          repoLimiter(async () => {
            if (onProgress) onProgress(`拉取 ${repoName} / ${username}...`);
            const prs = await fetchRepoUserData(
              repoCfg,
              username,
              token,
              detailLimiter,
              onProgress,
              fetchMode,
              fetchRange,
              fetchStates
            );
            data[repoName][username] = prs || [];
          })
        );
      });
    });
    await Promise.all(tasks);
    const totalPrs = Object.values(data).reduce((accRepo, repoUsers) => {
      return accRepo + Object.values(repoUsers || {}).reduce((acc, prs) => acc + (prs ? prs.length : 0), 0);
    }, 0);
    logInfo('拉取完成', { totalPrs });
    return data;
  };

  const refreshFromApi = async () => {
    const inputToken = (tokenInput?.value || '').trim();
    const token = inputToken || readToken();
    if (!token) {
      setTokenStatus('', 'error');
      alert('请先设置 API Token');
      return;
    }
    if (inputToken) saveToken(inputToken);
    setTokenStatus(token);

    const btnLabel = refreshBtn ? refreshBtn.textContent : '';
    if (refreshBtn) {
      refreshBtn.disabled = true;
      refreshBtn.textContent = '刷新中...';
    }
    const fetchMode = normalizeFetchMode(fetchModeSelect?.value || '');
    const fetchRange = buildFetchRange();
    const fetchStates = getFetchStates();
    if (!fetchStates.length) {
      setRefreshStatus('未选择抓取状态', 'error');
      alert('请至少选择一个抓取状态（open / merged）');
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.textContent = btnLabel || '刷新数据';
      }
      return;
    }
    setRefreshStatus('正在拉取数据...', 'ok');
    logInfo('刷新开始', {
      fetchMode,
      fetchField: fetchRange.field,
      fetchStates,
      fetchStart: fetchRange.start,
      fetchEnd: fetchRange.end,
    });
    try {
      const data = await fetchAllData(
        token,
        (msg) => setRefreshStatus(msg, 'ok'),
        fetchMode,
        fetchRange,
        fetchStates
      );
      const meta = collectMetaFromData(data);
      renderDynamicFilters(meta);
      buildCardView(data);
      saveCachedData(data);
      wrappedApply();
      logInfo('筛选项数量', {
        issueLabels: (meta.issueLabels || []).length,
        prTypes: (meta.prTypes || []).length,
        targets: (meta.targets || []).length,
      });
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
      setRefreshStatus(`已刷新 ${stamp}`, 'ok');
      if (refreshStamp) refreshStamp.textContent = `刷新：${stamp}`;
      try { localStorage.setItem('pr_report_last_refresh_v1', stamp); } catch (e) {}
      logInfo('刷新完成', { stamp });
    } catch (e) {
      console.error(e);
      setRefreshStatus('刷新失败，请检查 Token 或网络', 'error');
      alert(`刷新失败：${e?.message || e}`);
      logInfo('刷新失败', e?.message || e);
    } finally {
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.textContent = btnLabel || '刷新数据';
      }
    }
  };

  const openSettings = () => {
    if (!settingsModal) return;
    if (fetchModal) fetchModal.dataset.open = '0';
    settingsModal.dataset.open = '1';
  };
  const closeSettings = () => {
    if (!settingsModal) return;
    settingsModal.dataset.open = '0';
  };
  const openFetch = () => {
    if (!fetchModal) return;
    if (settingsModal) settingsModal.dataset.open = '0';
    fetchModal.dataset.open = '1';
  };
  const closeFetch = () => {
    if (!fetchModal) return;
    fetchModal.dataset.open = '0';
  };
  if (filterToggle) {
    filterToggle.addEventListener('click', openSettings);
  }
  if (fetchToggle) {
    fetchToggle.addEventListener('click', openFetch);
  }
  if (headerMenuBtn) {
    headerMenuBtn.addEventListener('click', openSettings);
  }
  if (settingsBackdrop) {
    settingsBackdrop.addEventListener('click', closeSettings);
  }
  if (settingsCloseBtn) {
    settingsCloseBtn.addEventListener('click', closeSettings);
  }
  if (fetchBackdrop) {
    fetchBackdrop.addEventListener('click', closeFetch);
  }
  if (fetchCloseBtn) {
    fetchCloseBtn.addEventListener('click', closeFetch);
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeSettings();
      closeFetch();
    }
  });

  // 导出 CSV
  const exportBtn = document.getElementById('export-csv');
  if (exportBtn) {
    exportBtn.addEventListener('click', () => {
      const rows = collectVisibleCards();
      if (!rows.length) {
        alert('当前筛选没有 PR 可导出');
        return;
      }
      const header = [
        'repo',
        'user',
        'pr_number',
        'title',
        'url',
        'state',
        'unresolved',
        'resolved',
        'additions',
        'deletions',
        'files',
        'ext_summary',
        'created',
        'updated',
        'branch',
        'pr_type',
        'issue_labels',
      ];
      const csvRows = [header.join(',')];
      const escape = (v) => {
        const str = (v ?? '').toString().replace(/"/g, '""');
        if (str.includes(',') || str.includes('"')) return `"${str}"`;
        return str;
      };
      rows.forEach((r) => {
        const line = [
          escape(r.repo),
          escape(r.user),
          escape(r.num),
          escape(r.title),
          escape(r.url),
          escape(r.state),
          escape(r.unresolved),
          escape(r.resolved),
          escape(r.codeKnown ? r.additions : ''),
          escape(r.codeKnown ? r.deletions : ''),
          escape(r.codeKnown ? r.files : ''),
          escape(r.codeKnown ? (r.extSummary || '') : ''),
          escape(r.created),
          escape(r.updated),
          escape(r.branch),
          escape(r.type),
          escape(r.labels.join(';')),
        ];
        csvRows.push(line.join(','));
      });
      const blob = new Blob([csvRows.join('\\n')], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      a.download = `pr-report-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  const exportCsv = (filename, header, dataRows) => {
    const escape = (v) => {
      const str = (v ?? '').toString().replace(/"/g, '""');
      if (str.includes(',') || str.includes('"') || str.includes('\\n')) return `"${str}"`;
      return str;
    };
    const csvRows = [header.map(escape).join(',')];
    (dataRows || []).forEach((r) => {
      csvRows.push(header.map((k) => escape(r[k])).join(','));
    });
    const blob = new Blob([csvRows.join('\\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const getCommentUrl = (prUrl, commentId) => {
    if (!prUrl) return '';
    if (!commentId) return prUrl;
    // GitCode 通常兼容 GitLab 的 note anchor；即使不命中也会打开 PR 页面
    return `${prUrl}#note_${commentId}`;
  };

  if (exportReviewBtn) {
    exportReviewBtn.addEventListener('click', () => {
      const now = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const ymd = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;

      if (currentViewMode !== 'issue' && currentViewMode !== 'received') {
        alert('请先切换到“提出”或“被提”视图再导出检视意见 CSV');
        return;
      }
      const header = [
        'mode',
        'group_user',
        'repo',
        'pr_number',
        'pr_title',
        'pr_url',
        'comment_id',
        'comment_url',
        'resolved',
        'review_author',
        'pr_author',
        'excerpt',
      ];

      if (currentViewMode === 'issue') {
        const details = collectVisibleIssues();
        if (!details.length) {
          alert('当前筛选下无检视意见（主评论）可导出');
          return;
        }
        const rows = details.map((d) => ({
          mode: 'issue',
          group_user: d.user || '',
          repo: d.repo || '',
          pr_number: d.prNum || '',
          pr_title: d.prTitle || '',
          pr_url: d.prUrl || '',
          comment_id: d.commentId || '',
          comment_url: getCommentUrl(d.prUrl || '', d.commentId || ''),
          resolved: d.resolved ? 'true' : 'false',
          review_author: d.user || '',
          pr_author: d.prAuthor || '',
          excerpt: d.excerpt || '',
        }));
        exportCsv(`review-comments-issued-${ymd}.csv`, header, rows);
        return;
      }

      const details = collectVisibleReceived();
      if (!details.length) {
        alert('当前筛选下无被提检视意见（主评论）可导出');
        return;
      }
      const rows = details.map((d) => ({
        mode: 'received',
        group_user: d.prAuthor || '',
        repo: d.repo || '',
        pr_number: d.prNum || '',
        pr_title: d.prTitle || '',
        pr_url: d.prUrl || '',
        comment_id: d.commentId || '',
        comment_url: getCommentUrl(d.prUrl || '', d.commentId || ''),
        resolved: d.resolved ? 'true' : 'false',
        review_author: d.reviewer || '',
        pr_author: d.prAuthor || '',
        excerpt: d.excerpt || '',
      }));
      exportCsv(`review-comments-received-${ymd}.csv`, header, rows);
    });
  }

  // 视图切换
  const VIEW_KEY = 'pr_report_view_mode_v1';
  let currentViewMode = 'card';
  const setView = (mode, opts = {}) => {
    const nextMode =
      (mode === 'list' || mode === 'issue' || mode === 'received' || mode === 'code')
        ? mode
        : 'card';
    currentViewMode = nextMode;
    if (!opts.skipPersist) {
      try { localStorage.setItem(VIEW_KEY, nextMode); } catch (e) {}
    }
    const showReviewControls = nextMode === 'issue' || nextMode === 'received';
    if (reviewControls) {
      reviewControls.dataset.show = showReviewControls ? '1' : '0';
    }
    if (reviewUserKeyword) {
      reviewUserKeyword.placeholder = nextMode === 'received' ? '筛选被提人（PR 作者）' : '筛选提出人';
    }
    if (nextMode === 'list') {
      if (!cardView || !listView) return;
      cardView.style.display = 'none';
      listView.style.display = 'block';
      if (issueView) issueView.style.display = 'none';
      if (receivedView) receivedView.style.display = 'none';
      if (codeView) codeView.style.display = 'none';
      if (viewListBtn) viewListBtn.classList.add('active');
      if (viewCardBtn) viewCardBtn.classList.remove('active');
      if (viewIssueBtn) viewIssueBtn.classList.remove('active');
      if (viewReceivedBtn) viewReceivedBtn.classList.remove('active');
      if (viewCodeBtn) viewCodeBtn.classList.remove('active');
      refreshListView();
    } else if (nextMode === 'issue') {
      if (cardView) cardView.style.display = 'none';
      if (listView) listView.style.display = 'none';
      if (issueView) issueView.style.display = 'block';
      if (receivedView) receivedView.style.display = 'none';
      if (codeView) codeView.style.display = 'none';
      if (viewIssueBtn) viewIssueBtn.classList.add('active');
      if (viewCardBtn) viewCardBtn.classList.remove('active');
      if (viewListBtn) viewListBtn.classList.remove('active');
      if (viewReceivedBtn) viewReceivedBtn.classList.remove('active');
      if (viewCodeBtn) viewCodeBtn.classList.remove('active');
      refreshIssueView();
    } else if (nextMode === 'received') {
      if (cardView) cardView.style.display = 'none';
      if (listView) listView.style.display = 'none';
      if (issueView) issueView.style.display = 'none';
      if (receivedView) receivedView.style.display = 'block';
      if (codeView) codeView.style.display = 'none';
      if (viewReceivedBtn) viewReceivedBtn.classList.add('active');
      if (viewCardBtn) viewCardBtn.classList.remove('active');
      if (viewListBtn) viewListBtn.classList.remove('active');
      if (viewIssueBtn) viewIssueBtn.classList.remove('active');
      if (viewCodeBtn) viewCodeBtn.classList.remove('active');
      refreshReceivedView();
    } else if (nextMode === 'code') {
      if (cardView) cardView.style.display = 'none';
      if (listView) listView.style.display = 'none';
      if (issueView) issueView.style.display = 'none';
      if (receivedView) receivedView.style.display = 'none';
      if (codeView) codeView.style.display = 'block';
      if (viewCodeBtn) viewCodeBtn.classList.add('active');
      if (viewCardBtn) viewCardBtn.classList.remove('active');
      if (viewListBtn) viewListBtn.classList.remove('active');
      if (viewIssueBtn) viewIssueBtn.classList.remove('active');
      if (viewReceivedBtn) viewReceivedBtn.classList.remove('active');
      refreshCodeView();
    } else {
      if (!cardView || !listView) return;
      cardView.style.display = 'block';
      listView.style.display = 'none';
      if (issueView) issueView.style.display = 'none';
      if (receivedView) receivedView.style.display = 'none';
      if (codeView) codeView.style.display = 'none';
      if (viewCardBtn) viewCardBtn.classList.add('active');
      if (viewListBtn) viewListBtn.classList.remove('active');
      if (viewIssueBtn) viewIssueBtn.classList.remove('active');
      if (viewReceivedBtn) viewReceivedBtn.classList.remove('active');
      if (viewCodeBtn) viewCodeBtn.classList.remove('active');
    }
  };
  if (viewCardBtn) {
    viewCardBtn.addEventListener('click', () => setView('card'));
  }
  if (viewListBtn) {
    viewListBtn.addEventListener('click', () => setView('list'));
  }
  if (viewIssueBtn) {
    viewIssueBtn.addEventListener('click', () => setView('issue'));
  }
  if (viewReceivedBtn) {
    viewReceivedBtn.addEventListener('click', () => setView('received'));
  }
  if (viewCodeBtn) {
    viewCodeBtn.addEventListener('click', () => setView('code'));
  }
  const refreshActiveReviewView = () => {
    if (currentViewMode === 'issue') refreshIssueView();
    if (currentViewMode === 'received') refreshReceivedView();
  };
  let reviewDebounce = null;
  if (reviewUserKeyword) {
    reviewUserKeyword.addEventListener('input', () => {
      if (reviewDebounce) clearTimeout(reviewDebounce);
      reviewDebounce = setTimeout(refreshActiveReviewView, 160);
    });
  }
  if (reviewSortSelect) {
    reviewSortSelect.addEventListener('change', refreshActiveReviewView);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      refreshFromApi();
    });
  }

  // 下拉面板开关
  const closeAllDropdowns = () => {
    document.querySelectorAll('.filter-user-panel').forEach((panel) => {
      panel.classList.remove('open');
    });
  };
  const bindDropdown = (toggleEl, panelEl, wrapper) => {
    if (!toggleEl || !panelEl) return;
    toggleEl.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = panelEl.classList.contains('open');
      closeAllDropdowns();
      if (!isOpen) {
        panelEl.classList.add('open');
      }
    });
    if (wrapper) {
      wrapper.addEventListener('click', (e) => e.stopPropagation());
    }
  };
  bindDropdown(userToggle, userPanel, userDropdown);
  bindDropdown(groupToggle, groupPanel, groupDropdown);
  document.addEventListener('click', () => closeAllDropdowns());

  // 更新 summary 时机
  wrappedApply = () => {
    applyFilters();
    updateSummary();
    if (listView && listView.style.display !== 'none') {
      refreshListView();
    }
    if (issueView && issueView.style.display !== 'none') {
      refreshIssueView();
    }
    if (receivedView && receivedView.style.display !== 'none') {
      refreshReceivedView();
    }
    if (codeView && codeView.style.display !== 'none') {
      refreshCodeView();
    }
    refreshStats();
  };
  bindUserGroupListeners = () => {
    userChecks.forEach((c) => {
      c.removeEventListener('change', applyFilters);
      c.addEventListener('change', wrappedApply);
    });
    groupChecks.forEach((c) => {
      c.removeEventListener('change', applyFilters);
      c.addEventListener('change', wrappedApply);
    });
    refreshUserToggleText(getSelectedUsers());
    refreshGroupToggleText(getSelectedGroups());
  };
  bindUserGroupListeners();

  // 评论关键字输入，轻量防抖
  let keywordDebounce = null;
  if (filterCommentKeyword) {
    filterCommentKeyword.addEventListener('input', () => {
      if (keywordDebounce) {
        clearTimeout(keywordDebounce);
      }
      keywordDebounce = setTimeout(wrappedApply, 180);
    });
  }
  if (filterCommentExclude) {
    filterCommentExclude.addEventListener('input', () => {
      if (keywordDebounce) {
        clearTimeout(keywordDebounce);
      }
      keywordDebounce = setTimeout(wrappedApply, 180);
    });
  }

  // 替换之前绑定
  filterUnresolved.removeEventListener('change', applyFilters);
  filterUnresolved.addEventListener('change', wrappedApply);
  filterHideClean.removeEventListener('change', applyFilters);
  filterHideClean.addEventListener('change', wrappedApply);
  if (sortSelect) {
    sortSelect.addEventListener('change', wrappedApply);
  }
  if (quickOpenUnresolvedBtn) {
    quickOpenUnresolvedBtn.addEventListener('click', () => {
      stateChecks.forEach((c) => {
        c.checked = c.value === 'open';
      });
      commentChecks.forEach((c) => {
        c.checked = c.value === 'unresolved';
      });
      if (filterUnresolved) filterUnresolved.checked = true;
      if (filterHideClean) filterHideClean.checked = true;
      wrappedApply();
    });
  }
  stateChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
  commentChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
  targetChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
  issueLabelChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
  prTypeChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
  if (filterHideEmptyUsers) {
    filterHideEmptyUsers.removeEventListener('change', applyFilters);
    filterHideEmptyUsers.addEventListener('change', wrappedApply);
  }
  if (filterHideReplies) {
    filterHideReplies.removeEventListener('change', applyFilters);
    filterHideReplies.addEventListener('change', wrappedApply);
  }
  if (filterResolvedOnly) {
    filterResolvedOnly.removeEventListener('change', applyFilters);
    filterResolvedOnly.addEventListener('change', wrappedApply);
  }
  if (filterDateStart) {
    filterDateStart.removeEventListener('change', applyFilters);
    filterDateStart.addEventListener('change', wrappedApply);
  }
  if (filterDateEnd) {
    filterDateEnd.removeEventListener('change', applyFilters);
    filterDateEnd.addEventListener('change', wrappedApply);
  }
  if (filterDateField) {
    filterDateField.addEventListener('change', wrappedApply);
  }
  if (userSelectAllBtn) {
    userSelectAllBtn.removeEventListener('click', applyFilters);
    userSelectAllBtn.addEventListener('click', () => {
      userChecks.forEach((c) => (c.checked = true));
      wrappedApply();
    });
  }
  if (userSelectNoneBtn) {
    userSelectNoneBtn.removeEventListener('click', applyFilters);
    userSelectNoneBtn.addEventListener('click', () => {
      userChecks.forEach((c) => (c.checked = false));
      wrappedApply();
    });
  }
  if (groupSelectAllBtn) {
    groupSelectAllBtn.removeEventListener('click', applyFilters);
    groupSelectAllBtn.addEventListener('click', () => {
      groupChecks.forEach((c) => (c.checked = true));
      wrappedApply();
    });
  }
  if (groupSelectNoneBtn) {
    groupSelectNoneBtn.removeEventListener('click', applyFilters);
    groupSelectNoneBtn.addEventListener('click', () => {
      groupChecks.forEach((c) => (c.checked = false));
      wrappedApply();
    });
  }

  const hydrateFromCache = () => {
    if (!cardView || cardView.querySelector('.pr-card')) return false;
    const cached = loadCachedData();
    if (!cached) return false;
    const meta = collectMetaFromData(cached);
    renderDynamicFilters(meta);
    buildCardView(cached);
    logInfo('已加载缓存数据');
    return true;
  };

  // 初始执行：默认结束日期为当天
  if (filterDateEnd && !filterDateEnd.value) {
    const today = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const y = today.getFullYear();
    const m = pad(today.getMonth() + 1);
    const d = pad(today.getDate());
    filterDateEnd.value = `${y}-${m}-${d}`;
  }
  // 日期按钮弹出日历
  document.querySelectorAll('.date-picker-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const map = {
        start: 'filter-date-start',
        end: 'filter-date-end',
        'fetch-start': 'fetch-date-start',
        'fetch-end': 'fetch-date-end',
      };
      const targetId = map[btn.dataset.picker] || 'filter-date-start';
      const input = document.getElementById(targetId);
      if (!input) return;
      if (typeof input.showPicker === 'function') {
        input.showPicker();
      } else {
        input.focus();
        input.click();
      }
    });
  });
  hydrateFromCache();
  wrappedApply();
  try {
    const savedView = localStorage.getItem(VIEW_KEY) || '';
    if (savedView) {
      setView(savedView, { skipPersist: true });
    }
  } catch (e) {}
})();
</script>
"""

    html_parts.append(
        script.replace("__GROUP_MEMBERS__", group_json).replace(
            "__CLIENT_CONFIG__", client_config_json
        )
    )

    html_parts.append(
        f"<div class='footer'>由自动脚本生成 · 数据来源：GitCode API · 执行时间：{escape_html(executed_at)}</div>"
    )
    html_parts.append("</div></body></html>")

    return "\n".join(html_parts)


# ----------------- main -----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 GitCode PR 检视 HTML 报表（适合部署到 GitHub Pages）"
    )
    parser.add_argument(
        "-c",
        "--config",
        default=".github/gitcode_pr_config.toml",
        help="配置文件路径（默认 .github/gitcode_pr_config.toml）",
    )
    parser.add_argument(
        "--only-unresolved",
        action="store_true",
        help="页面默认只展示未解决的检视意见（可在页面上切换）",
    )
    parser.add_argument(
        "--hide-clean-prs",
        action="store_true",
        help="页面默认隐藏没有未解决检视意见且已关闭的 PR（可在页面上切换）",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="site/index.html",
        help="输出 HTML 路径（默认 site/index.html）",
    )
    parser.add_argument(
        "--client-only",
        action="store_true",
        help="仅生成前端页面骨架，不在服务端拉取 PR 数据",
    )
    parser.add_argument(
        "--no-code-stats",
        action="store_true",
        help="不在服务端计算代码变更统计（additions/deletions/files）",
    )

    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        sys.exit(1)

    client_only = args.client_only or os.getenv("GITCODE_CLIENT_ONLY") == "1"
    code_stats_enabled = cfg.code_stats and not args.no_code_stats
    print(
        "[info] options:",
        f"client_only={client_only}",
        f"code_stats_enabled={code_stats_enabled}",
        f"repos={len(cfg.repos)}",
        f"users={len(cfg.users)}",
    )

    if not cfg.access_token and not client_only:
        print(
            "警告：未配置 access_token，私有仓或配额受限的情况下 API 可能失败。\n"
            "你可以在配置文件中设置 access_token，或者导出环境变量 GITCODE_TOKEN。",
            file=sys.stderr,
        )

    # { repo_name -> { username -> [PRInfo] } }
    repo_user_prs: Dict[str, Dict[str, List[PRInfo]]] = {}

    if not client_only:
        # 先把所有 (repo_cfg, username) 任务列出来
        tasks = []
        for repo_cfg in cfg.repos:
            repo_name = f"{repo_cfg.owner}/{repo_cfg.repo}"
            for username in cfg.users:
                tasks.append((repo_name, repo_cfg, username))
    else:
        tasks = []

    # 执行时间（Asia/Shanghai）
    executed_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )

    if tasks:
        # 并发执行，max_workers 可以按你仓库/用户规模调，8–16 一般够
        max_workers = min(len(tasks), 16) or 1
        print(f"[info] server fetch tasks: {len(tasks)}")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_key = {}
            for repo_name, repo_cfg, username in tasks:
                fut = executor.submit(
                    fetch_repo_user_data,
                    cfg.access_token,
                    repo_cfg,
                    username,
                    code_stats_enabled=code_stats_enabled,
                )
                future_to_key[fut] = (repo_name, username)

            for fut in as_completed(future_to_key):
                repo_name, username = future_to_key[fut]
                try:
                    prs = fut.result()
                except Exception as e:
                    print(
                        f"\n!!! 获取 {repo_name} 中 {username} 的 PR 时出错: {e}",
                        file=sys.stderr,
                    )
                    prs = []

                repo_user_prs.setdefault(repo_name, {})[username] = prs

    # 生成 HTML
    html = build_html(
        cfg,
        repo_user_prs,
        default_only_unresolved=args.only_unresolved,
        default_hide_clean_prs=args.hide_clean_prs,
        executed_at=executed_at,
    )

    out_path = args.output
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"已生成报表: {out_path}")


if __name__ == "__main__":
    main()
