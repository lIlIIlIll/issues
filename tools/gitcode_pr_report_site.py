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
        access_token=access_token, users=merged_users, groups=groups, repos=repos
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
) -> List[PRInfo]:
    """
    拉取一个仓库 + 一个用户的所有 PR，并填充 issues/comments，
    不在拉取阶段做过滤，交给前端页面自行过滤。
    """
    prs = fetch_prs_for_user(access_token, repo_cfg, username)

    result: List[PRInfo] = []
    for pr in prs:
        # 先拉评论
        comments = fetch_comments_for_pr(access_token, repo_cfg, pr.number)
        pr.comments = comments

        # 再拉 issues
        pr.issues = fetch_issues_for_pr(access_token, repo_cfg, pr.number)

        result.append(pr)

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
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 0;
      background: #0f172a;
      color: #e5e7eb;
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
    .sub-title {
      font-size: 14px;
      color: #9ca3af;
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
      border: 1px solid #1f2937;
      background: #020617;
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
      color: #9ca3af;
      margin-left: 8px;
    }
    .repo-chevron {
      font-size: 12px;
      color: #6b7280;
      transition: transform 0.15s ease-out;
    }
    .repo-block[open] .repo-chevron {
      transform: rotate(90deg);
    }

    .repo-content {
      padding: 0 12px 10px 12px;
      border-top: 1px solid #1f2937;
    }

    .user-block {
      margin-top: 8px;
      margin-bottom: 10px;
      border-radius: 10px;
      border: 1px solid #1f2937;
      background: #020617;
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
      color: #9ca3af;
      margin-left: 8px;
    }
    .user-chevron {
      font-size: 11px;
      color: #6b7280;
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
      background: #111827;
      border-radius: 12px;
      padding: 12px 14px;
      border: 1px solid #1f2937;
      box-shadow: 0 10px 25px rgba(0,0,0,0.35);
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
      color: #9ca3af;
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
      color: #e5e7eb;  /* 默认浅灰白 */
    }

    .pr-branch {
      font-size: 12px;
      color: #cbd5f5;
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
      background: #111827;
      border: 1px solid #1f2937;
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

    .pr-times {
      font-size: 11px;
      color: #9ca3af;
      margin-bottom: 4px;
    }
    .pr-link {
      font-size: 11px;
      color: #60a5fa;
      text-decoration: none;
    }
    .pr-link-inline, .issue-link {
      color: #60a5fa;
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
      color: #e5e7eb;
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
      background: #1b2535;
      border: 1px solid #2a3548;
      box-shadow: 0 2px 6px rgba(0,0,0,0.35);
    }
    .review-item.unresolved {
      border-left: 4px solid #ef4444; /* 未解决：红色边 */
      background: rgba(239, 68, 68, 0.10);
    }
    .review-item.resolved {
      border-left: 4px solid #22c55e; /* 已解决：绿边 */
      background: rgba(34, 197, 94, 0.08);
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
      color: #9ca3af;
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
      color: #60a5fa;
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
      color: #6b7280;
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
      background: #0b1220;
      padding: 0 3px;
      border-radius: 3px;
      border: 1px solid #1f2937;
    }
    .review-code-block {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      background: #020617;
      border-radius: 6px;
      border: 1px solid #1f2937;
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
      border-top: 1px dashed #1f2937;
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
      color: #e5e7eb;
      display: flex;
      align-items: baseline;
    }
    .reviewer-group-title span {
      font-size: 11px;
      color: #9ca3af;
      margin-left: 8px;
    }
    .reviewer-chevron {
      font-size: 10px;
      color: #6b7280;
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
    .filter-toggle {
      border: 1px solid #334155;
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
    }
    .filter-toggle:hover {
      border-color: #60a5fa;
      color: #bfdbfe;
    }
    .filter-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .filter-chip-btn.secondary {
      background: #0b1220;
    }
    .filter-select {
      background: #0b1220;
      color: #e5e7eb;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 13px;
    }
    .view-toggle-btn {
      border: 1px solid #334155;
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
    }
    .view-toggle-btn.active {
      border-color: #60a5fa;
      background: #1f2937;
      color: #bfdbfe;
    }
    .filter-summary {
      font-size: 12px;
      color: #9ca3af;
      flex: 1;
    }

    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      padding: 14px;
      margin: 8px 0 22px;
      border-radius: 10px;
      border: 1px solid #1f2937;
      background: #0b1220;
    }
    .filter-group {
      flex: 1 1 260px;
      border: 1px solid #1f2937;
      border-radius: 8px;
      padding: 10px 12px;
      background: #0a101e;
    }
    .filter-group h3 {
      margin: 0 0 6px;
      font-size: 13px;
      color: #cbd5e1;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-group h3 span {
      font-size: 11px;
      color: #94a3b8;
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
      accent-color: #60a5fa;
      width: 16px;
      height: 16px;
    }
    .filter-hint {
      font-size: 12px;
      color: #9ca3af;
    }
    .filter-dates {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
    }
    .filter-dates input[type="date"] {
      background: #0b1220;
      color: #e5e7eb;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 4px 6px;
    }
    .date-picker-btn {
      border: 1px solid #334155;
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .date-picker-btn:hover {
      border-color: #60a5fa;
      color: #bfdbfe;
    }
    .date-quick-btn {
      border: 1px solid #334155;
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .date-quick-btn:hover {
      border-color: #60a5fa;
      color: #bfdbfe;
    }
    .filter-users {
      position: relative;
      display: inline-block;
    }
    .filter-user-toggle {
      border: 1px solid #334155;
      background: #0b1220;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 6px 10px;
      cursor: pointer;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-user-toggle:hover {
      border-color: #60a5fa;
      color: #bfdbfe;
    }
    .filter-user-panel {
      position: absolute;
      left: 0;
      top: calc(100% + 6px);
      min-width: 240px;
      background: #0b1220;
      border: 1px solid #1f2937;
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
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
      background: #0a101e;
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
      scrollbar-color: #334155 #0a101e;
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
      border: 1px solid #334155;
      background: #1f2937;
      color: #e5e7eb;
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .filter-chip-btn:hover {
      border-color: #60a5fa;
      color: #bfdbfe;
    }
    .list-view {
      display: none;
      margin-top: 12px;
    }
    .list-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    .list-table th,
    .list-table td {
      border: 1px solid #1f2937;
      padding: 8px;
      text-align: left;
    }
    .list-table th {
      background: #0b1220;
      color: #cbd5e1;
    }
    .list-table tr:nth-child(even) {
      background: #0a101e;
    }
    .list-table a {
      color: #93c5fd;
    }
    .stats-block {
      margin-top: 12px;
      border: 1px solid #1f2937;
      border-radius: 10px;
      padding: 10px 12px;
      background: #0a101e;
    }
    .stats-block h3 {
      margin: 0 0 8px;
      font-size: 13px;
      color: #cbd5e1;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      font-size: 12px;
      color: #e5e7eb;
    }
    .stats-item {
      background: #0b1220;
      border: 1px solid #1f2937;
      border-radius: 8px;
      padding: 8px;
    }

    .empty-text {
      font-size: 12px;
      color: #6b7280;
      margin-top: 4px;
    }
    .footer {
      margin-top: 40px;
      font-size: 11px;
      color: #6b7280;
      text-align: center;
    }
    """

    group_json = json.dumps(cfg.groups, ensure_ascii=False)

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
        f"<h1>{escape_html(title)}</h1>",
    ]

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
        "<button type='button' class='filter-toggle' id='filter-toggle'>收起筛选</button>"
    )
    html_parts.append(
        "<div class='filter-summary' id='filter-summary'>当前筛选：全部</div>"
    )
    html_parts.append("</div>")
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
        "<div style='display:flex;gap:6px'>"
        "<button type='button' class='view-toggle-btn active' id='view-card-btn'>卡片视图</button>"
        "<button type='button' class='view-toggle-btn' id='view-list-btn'>列表视图</button>"
        "</div>"
    )
    html_parts.append(
        "<button type='button' class='filter-chip-btn secondary' id='export-csv'>导出当前筛选 CSV</button>"
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
        "<button type='button' class='filter-chip-btn secondary' id='refresh-data'>刷新数据</button>"
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
    html_parts.append(
        "<label class='filter-label'>"
        "<input type='checkbox' id='filter-hide-replies' />"
        " 不展示回复（仅显示主评论）"
        "</label>"
    )
    html_parts.append("</div>")

    # Issue 标签
    if issue_labels:
        html_parts.append("<div class='filter-group'>")
        html_parts.append("<h3>Issue 标签 <span>(多选)</span></h3>")
        html_parts.append("<div class='filter-user-list'>")
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
    if pr_types:
        html_parts.append("<div class='filter-group'>")
        html_parts.append("<h3>PR 类型 <span>(title 前缀，多选)</span></h3>")
        html_parts.append("<div class='filter-user-list'>")
        for t in pr_types:
            html_parts.append(
                "<label class='filter-label'>"
                f"<input type='checkbox' class='filter-pr-type-checkbox' value='{escape_html(t)}' /> "
                f"{escape_html(t)}"
                "</label>"
            )
        html_parts.append("</div>")
        html_parts.append("</div>")

    if target_branches:
        html_parts.append("<div class='filter-group'>")
        html_parts.append("<h3>目标分支 <span>(多选)</span></h3>")
        html_parts.append("<div class='filter-user-list'>")
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
        "<span>创建日期：</span>"
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
    if cfg.users:
        html_parts.append("<div class='filter-users' id='filter-user-dropdown'>")
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
        html_parts.append("<div class='filter-user-list'>")
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
    if cfg.groups:
        html_parts.append("<div class='filter-users' id='filter-group-dropdown'>")
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
        html_parts.append("<div class='filter-user-list'>")
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
    html_parts.append("</div>")  # filter-container

    # 统计概览
    html_parts.append("<div class='stats-block' id='stats-block'>")
    html_parts.append("<h3>当前筛选统计</h3>")
    html_parts.append("<div class='stats-grid'>")
    html_parts.append(
        "<div class='stats-item'>总计：<span id='stat-total'>0</span></div>"
    )
    html_parts.append(
        "<div class='stats-item'>open：<span id='stat-open'>0</span></div>"
    )
    html_parts.append(
        "<div class='stats-item'>merged：<span id='stat-merged'>0</span></div>"
    )
    html_parts.append(
        "<div class='stats-item'>有未解决意见：<span id='stat-unresolved'>0</span></div>"
    )
    html_parts.append("</div>")
    html_parts.append("</div>")

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
                        unresolved_comments = [
                            cm for cm in all_comments if cm.resolved is False
                        ]
                        unresolved_count = len(unresolved_comments)
                        resolved_count = len(
                            [cm for cm in all_comments if cm.resolved is True]
                        )

                        issue_labels_flat: List[str] = []
                        for iss in pr.issues:
                            for lab in iss.labels:
                                if not lab:
                                    continue
                                if lab not in issue_labels_flat:
                                    issue_labels_flat.append(lab)

                        pr_type = _infer_pr_type(pr.title or "")

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
                            # 1. 按 reviewer 分组（保留原有顺序）
                            from collections import OrderedDict

                            grouped: "OrderedDict[str, List[ReviewComment]]" = (
                                OrderedDict()
                            )
                            for cm in all_comments:
                                key = cm.user or "(unknown)"
                                if key not in grouped:
                                    grouped[key] = []
                                grouped[key].append(cm)

                            # 2. 逐个 reviewer 输出
                            for reviewer, comments in grouped.items():
                                parent_comments = [
                                    cm for cm in comments if not cm.is_reply
                                ]
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

                                replies_by_parent: Dict[int, List[ReviewComment]] = {}
                                orphan_replies: List[ReviewComment] = []
                                for cm in comments:
                                    if cm.is_reply and cm.parent_id is not None:
                                        replies_by_parent.setdefault(
                                            cm.parent_id, []
                                        ).append(cm)
                                    elif cm.is_reply:
                                        orphan_replies.append(cm)

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

                                # 孤立回复也展示
                                for rp in orphan_replies:
                                    render_comment(rp, is_reply=True)

                                html_parts.append("</div>")  # reviewer-group-body
                                html_parts.append("</details>")  # reviewer-group

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
        "<th>仓库</th><th>用户</th><th>PR</th><th>状态</th><th>类型</th><th>未解决</th><th>已解决</th><th>创建</th><th>更新时间</th><th>分支</th>"
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
  const filterDateStart = document.getElementById('filter-date-start');
  const filterDateEnd = document.getElementById('filter-date-end');
  const filterBar = document.getElementById('filter-bar');
  const filterToggle = document.getElementById('filter-toggle');
  const filterSummary = document.getElementById('filter-summary');
  const sortSelect = document.getElementById('sort-select');
  const quickOpenUnresolvedBtn = document.getElementById('quick-open-unresolved');
  const cardView = document.getElementById('card-view');
  const listView = document.getElementById('list-view');
  const listTableBody = document.querySelector('#list-table tbody');
  const viewCardBtn = document.getElementById('view-card-btn');
  const viewListBtn = document.getElementById('view-list-btn');
  const presetSelect = document.getElementById('preset-select');
  const presetApplyBtn = document.getElementById('preset-apply');
  const presetSaveBtn = document.getElementById('preset-save');
  const refreshBtn = document.getElementById('refresh-data');
  const statTotal = document.getElementById('stat-total');
  const statOpen = document.getElementById('stat-open');
  const statMerged = document.getElementById('stat-merged');
  const statUnresolved = document.getElementById('stat-unresolved');
  const stateChecks = Array.from(document.querySelectorAll('.filter-state-checkbox'));
  const commentChecks = Array.from(document.querySelectorAll('.filter-comment-checkbox'));
  const issueLabelChecks = Array.from(document.querySelectorAll('.filter-issue-label-checkbox'));
  const prTypeChecks = Array.from(document.querySelectorAll('.filter-pr-type-checkbox'));
  const targetChecks = Array.from(document.querySelectorAll('.filter-target-checkbox'));
  const userChecks = Array.from(document.querySelectorAll('.filter-user-checkbox'));
  const userSelectAllBtn = document.getElementById('filter-user-all');
  const userSelectNoneBtn = document.getElementById('filter-user-none');
  const userToggle = document.getElementById('filter-user-toggle');
  const userPanel = document.getElementById('filter-user-panel');
  const userDropdown = document.getElementById('filter-user-dropdown');
  const groupChecks = Array.from(document.querySelectorAll('.filter-group-checkbox'));
  const groupSelectAllBtn = document.getElementById('filter-group-all');
  const groupSelectNoneBtn = document.getElementById('filter-group-none');
  const groupToggle = document.getElementById('filter-group-toggle');
  const groupPanel = document.getElementById('filter-group-panel');
  const groupDropdown = document.getElementById('filter-group-dropdown');
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

  const GROUP_MEMBERS = __GROUP_MEMBERS__;

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
      tr.innerHTML = `
        <td>${r.repo}</td>
        <td>${r.user}</td>
        <td>${prCell}</td>
        <td>${r.state}</td>
        <td>${r.type || ''}</td>
        <td>${r.unresolved}</td>
        <td>${r.resolved}</td>
        <td>${r.created}</td>
        <td>${r.updated}</td>
        <td>${r.branch}</td>
      `;
      listTableBody.appendChild(tr);
    });
  };

  const refreshStats = () => {
    if (!statTotal || !statOpen || !statMerged || !statUnresolved) return;
    const rows = collectVisibleCards();
    const total = rows.length;
    const openCnt = rows.filter((r) => (r.state || '').toLowerCase() === 'open').length;
    const mergedCnt = rows.filter((r) => (r.state || '').toLowerCase() === 'merged').length;
    const unresolvedCnt = rows.filter((r) => r.unresolved > 0).length;
    statTotal.textContent = total;
    statOpen.textContent = openCnt;
    statMerged.textContent = mergedCnt;
    statUnresolved.textContent = unresolvedCnt;
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
      commentKeyword: filterCommentKeyword?.value || '',
      commentExclude: filterCommentExclude?.value || '',
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
    if (filterCommentKeyword) filterCommentKeyword.value = snap.commentKeyword || '';
    if (filterCommentExclude) filterCommentExclude.value = snap.commentExclude || '';
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
    const selectedGroupUsers = new Set();
    if (selectedGroups) {
      selectedGroups.forEach((name) => {
        const arr = GROUP_MEMBERS[name] || [];
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
      const createdStr = card.dataset.created || '';
      const createdTs = Date.parse(createdStr);
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
    const parentUser = (it.dataset.parentUser || '').trim();
    const authorReplyOnly = !isReply || !parentUser || parentUser === user;
    const bodyNode =
      it.querySelector('.review-body') || it.querySelector('.review-body-content');
    const bodyText = (bodyNode ? bodyNode.textContent : it.textContent) || '';
    const excludeKw = (filterCommentExclude?.value || '').trim();
    const hasExclude = excludeKw.length > 0;
    const excludeHit = isReply && hasExclude && matchWholeWord(bodyText, excludeKw);
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
      authorReplyOnly;
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
    filterSummary.textContent = `当前筛选：状态(${statesText}) · 检视(${commentsText}) · 回复(${hideRepliesText}) · 回复包含(${keywordText}) · 回复不包含(${excludeText}) · 标签(${labelText}) · 类型(${prTypeText}) · 目标(${targetText}) · 日期(${datePart}) · ${hideEmpty} · 排序(${sortText})`;
  };

  if (filterToggle && filterBar) {
    filterToggle.addEventListener('click', () => {
      const isOpen = filterBar.dataset.open === '1';
      filterBar.style.display = isOpen ? 'none' : 'flex';
      filterBar.dataset.open = isOpen ? '0' : '1';
      filterToggle.textContent = isOpen ? '展开筛选' : '收起筛选';
    });
  }

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

  // 视图切换
  const setView = (mode) => {
    if (!cardView || !listView) return;
    if (mode === 'list') {
      cardView.style.display = 'none';
      listView.style.display = 'block';
      if (viewListBtn) viewListBtn.classList.add('active');
      if (viewCardBtn) viewCardBtn.classList.remove('active');
      refreshListView();
    } else {
      cardView.style.display = 'block';
      listView.style.display = 'none';
      if (viewCardBtn) viewCardBtn.classList.add('active');
      if (viewListBtn) viewListBtn.classList.remove('active');
    }
  };
  if (viewCardBtn) {
    viewCardBtn.addEventListener('click', () => setView('card'));
  }
  if (viewListBtn) {
    viewListBtn.addEventListener('click', () => setView('list'));
  }
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => window.location.reload());
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
  const wrappedApply = () => {
    applyFilters();
    updateSummary();
    if (listView && listView.style.display !== 'none') {
      refreshListView();
    }
    refreshStats();
  };

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
  if (filterDateStart) {
    filterDateStart.removeEventListener('change', applyFilters);
    filterDateStart.addEventListener('change', wrappedApply);
  }
  if (filterDateEnd) {
    filterDateEnd.removeEventListener('change', applyFilters);
    filterDateEnd.addEventListener('change', wrappedApply);
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
  userChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });
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
  groupChecks.forEach((c) => {
    c.removeEventListener('change', applyFilters);
    c.addEventListener('change', wrappedApply);
  });

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
      const targetId =
        btn.dataset.picker === 'start' ? 'filter-date-start' : 'filter-date-end';
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
  wrappedApply();
})();
</script>
"""

    html_parts.append(script.replace("__GROUP_MEMBERS__", group_json))

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

    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not cfg.access_token:
        print(
            "警告：未配置 access_token，私有仓或配额受限的情况下 API 可能失败。\n"
            "你可以在配置文件中设置 access_token，或者导出环境变量 GITCODE_TOKEN。",
            file=sys.stderr,
        )

    # { repo_name -> { username -> [PRInfo] } }
    repo_user_prs: Dict[str, Dict[str, List[PRInfo]]] = {}

    # 先把所有 (repo_cfg, username) 任务列出来
    tasks = []
    for repo_cfg in cfg.repos:
        repo_name = f"{repo_cfg.owner}/{repo_cfg.repo}"
        for username in cfg.users:
            tasks.append((repo_name, repo_cfg, username))

    # 执行时间（Asia/Shanghai）
    executed_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )

    # 并发执行，max_workers 可以按你仓库/用户规模调，8–16 一般够
    max_workers = min(len(tasks), 16) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {}
        for repo_name, repo_cfg, username in tasks:
            fut = executor.submit(
                fetch_repo_user_data,
                cfg.access_token,
                repo_cfg,
                username,
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
