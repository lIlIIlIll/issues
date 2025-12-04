#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
生成 GitCode PR 检视报表（HTML），支持：
- 多仓库、多用户、多 PR 状态
- 只看未解决检视意见 (--only-unresolved)
- 没有未解决检视意见的 PR 直接隐藏 (--hide-clean-prs)
- 输出一个静态 HTML，可直接部署到 GitHub Pages
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    with open(path, "rb") as f:
        data = tomllib.load(f)

    access_token = (
        data.get("access_token")
        or os.getenv("GITCODE_TOKEN")
        or os.getenv("GITCODE_PAT")
    )

    users = data.get("users")
    if not users or not isinstance(users, list):
        raise ValueError('配置文件必须包含 users 数组，例如: users = ["alice", "bob"]')

    global_states = _normalize_states(data, ["all"])

    global_per_page = int(data.get("per_page", 50))
    if global_per_page < 1 or global_per_page > 100:
        global_per_page = 50

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

    return Config(access_token=access_token, users=users, repos=repos)


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
                if is_wip_title(title):
                    continue
                # 有些 GitLab/GitCode 风格的接口还会给 work_in_progress/draft 字段
                if pr.get("work_in_progress") is True or pr.get("draft") is True:
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
                url=it.get("url", ""),
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


from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_repo_user_data(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    username: str,
    *,
    only_unresolved: bool,
    hide_clean_prs: bool,
) -> List[PRInfo]:
    """
    拉取一个仓库 + 一个用户的所有 PR，并填充 issues/comments，
    根据 only_unresolved/hide_clean_prs 做必要的过滤。
    """
    prs = fetch_prs_for_user(access_token, repo_cfg, username)

    result: List[PRInfo] = []
    for pr in prs:
        # 先拉评论
        comments = fetch_comments_for_pr(access_token, repo_cfg, pr.number)
        pr.comments = comments

        # 如果启用了 hide_clean_prs 且没有未解决的意见，直接跳过这个 PR，
        # 连 issues 都不查，省一次请求。
        has_unresolved = any(cm.resolved is False for cm in comments)
        if hide_clean_prs and pr.state != "open" and not has_unresolved:
            continue

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
            params={"page": page, "per_page": 100},
        )

        if not isinstance(data, list) or not data:
            break

        for c in data:
            user_obj = c.get("user") or {}
            login = (
                user_obj.get("login")
                or user_obj.get("username")
                or user_obj.get("name")
                or ""
            )

            comments.append(
                ReviewComment(
                    id=int(c.get("id", 0)),
                    user=login,
                    body=c.get("body", ""),
                    created_at=c.get("created_at", ""),
                    updated_at=c.get("updated_at", ""),
                    resolved=_infer_resolved(c),
                    path=c.get("path"),
                    position=c.get("position"),
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


def build_html(
    cfg: Config,
    data: Dict[str, Dict[str, List[PRInfo]]],
    *,
    only_unresolved: bool,
    hide_clean_prs: bool,
    executed_at: str,
) -> str:
    """
    data 结构：
      { "owner/repo": { "username": [PRInfo, ...], ... }, ... }
    """
    title = "GitCode PR Review Report"

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
      align-items: baseline;
      gap: 8px;
      margin-bottom: 4px;
    }
    .pr-title {
      font-size: 14px;
      font-weight: 600;
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
    .issue-item, .review-item {
      font-size: 11px;
      margin-bottom: 4px;
    }
    .review-body {
      font-size: 11px;
      margin-top: 2px;
      white-space: pre-wrap;
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

    flags = []
    if only_unresolved:
        flags.append("仅显示未解决检视意见")
    if hide_clean_prs:
        flags.append("隐藏没有未解决检视意见的 PR")
    flag_text = "，".join(flags) if flags else "显示所有包含检视意见状态的 PR"

    html_parts.append(f"<div class='sub-title'>模式：{escape_html(flag_text)}</div>")
    html_parts.append(
        f"<div class='sub-title'>执行时间：{escape_html(executed_at)}</div>"
    )

    if not data:
        html_parts.append("<p class='empty-text'>没有任何符合条件的 PR。</p>")
    else:
        for repo_name, users_prs in data.items():
            # 统计这个 repo 有多少 PR（过滤后）
            total_prs = sum(len(v) for v in users_prs.values())

            html_parts.append(f"<details class='repo-block' open>")
            html_parts.append("<summary>")
            html_parts.append(f"<div class='repo-title'>仓库：{escape_html(repo_name)}")
            html_parts.append(
                f"<span class='repo-meta'>共 {total_prs} 个匹配 PR · {len(users_prs)} 个用户</span>"
            )
            html_parts.append("</div>")
            html_parts.append("<div class='repo-chevron'>▶</div>")
            html_parts.append("</summary>")

            html_parts.append("<div class='repo-content'>")

            for username, prs in users_prs.items():
                if len(prs) == 0:
                    continue
                html_parts.append("<details class='user-block' open>")
                html_parts.append("<summary>")
                html_parts.append(
                    f"<div class='user-title'>用户：{escape_html(username)}"
                )
                html_parts.append(f"<span class='user-meta'>{len(prs)} 个 PR</span>")
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
                    for pr in prs:
                        unresolved_comments = [
                            cm for cm in pr.comments if cm.resolved is False
                        ]
                        unresolved_count = len(unresolved_comments)

                        if unresolved_count > 0:
                            badge_cls = "badge-danger"
                            badge_text = f"{unresolved_count} 未解决"
                        elif pr.comments:
                            badge_cls = "badge-ok"
                            badge_text = "无未解决检视意见"
                        else:
                            badge_cls = "badge-warn"
                            badge_text = "无检视意见"

                        html_parts.append("<div class='pr-card'>")

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
                        state = (pr.state or "").lower()
                        if state == "open":
                            state_cls = "state-open"
                        elif state == "merged":
                            state_cls = "state-merged"
                        else:
                            state_cls = "state-other"
                        html_parts.append(
                            "<div class='pr-meta'>状态："
                            f"<span class='state-label {state_cls}'>{escape_html(pr.state)}</span>"
                            "</div>"
                        )

                        if pr.target_branch:
                            branch_str = f"{pr.source_branch} → {pr.target_branch}"
                        else:
                            branch_str = pr.source_branch
                        if branch_str:
                            html_parts.append(
                                f"<div class='pr-branch'>分支：{escape_html(branch_str)}</div>"
                            )

                        times_line = f"创建：{escape_html(pr.created_at)}"
                        if pr.updated_at:
                            times_line += f" ｜ 更新：{escape_html(pr.updated_at)}"
                        html_parts.append(f"<div class='pr-times'>{times_line}</div>")

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

                        if only_unresolved:
                            filtered_comments = unresolved_comments
                        else:
                            filtered_comments = [
                                cm for cm in pr.comments if cm.resolved is not None
                            ]

                        if not filtered_comments:
                            if only_unresolved:
                                html_parts.append(
                                    "<div class='empty-text'>无未解决的检视意见</div>"
                                )
                            else:
                                html_parts.append(
                                    "<div class='empty-text'>无需要 resolved 状态的检视意见</div>"
                                )
                        else:
                            for cm in filtered_comments:
                                resolved_str = "已解决" if cm.resolved else "未解决"
                                loc = ""
                                if cm.path:
                                    loc = f"（{cm.path}"
                                    if cm.position is not None:
                                        loc += f":{cm.position}"
                                    loc += "）"

                                html_parts.append("<div class='review-item'>")
                                html_parts.append(
                                    f"<div><strong>{escape_html(cm.user)}</strong> · {escape_html(resolved_str)}{escape_html(loc)}</div>"
                                )
                                html_parts.append(
                                    f"<div class='review-meta'>创建：{escape_html(cm.created_at)} ｜ 更新：{escape_html(cm.updated_at)}</div>"
                                )
                                if cm.body:
                                    html_parts.append(
                                        f"<div class='review-body'>{escape_html(cm.body)}</div>"
                                    )
                                html_parts.append("</div>")  # review-item

                        html_parts.append("</div>")  # pr-card
                    html_parts.append("</div>")  # pr-grid

                html_parts.append("</div>")  # user-content
                html_parts.append("</details>")  # user-block

            html_parts.append("</div>")  # repo-content
            html_parts.append("</details>")  # repo-block

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
        help="只显示未解决的检视意见（resolved=False）",
    )
    parser.add_argument(
        "--hide-clean-prs",
        action="store_true",
        help="如果 PR 没有未解决的检视意见，则不显示该 PR",
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

    now_utc = datetime.now()
    # 执行时间（Asia/Shanghai）
    executed_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )

    # 并发执行，max_workers 可以按你仓库/用户规模调，8–16 一般够
    max_workers = min(len(tasks), 8) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {}
        for repo_name, repo_cfg, username in tasks:
            fut = executor.submit(
                fetch_repo_user_data,
                cfg.access_token,
                repo_cfg,
                username,
                only_unresolved=args.only_unresolved,
                hide_clean_prs=args.hide_clean_prs,
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
        only_unresolved=args.only_unresolved,
        hide_clean_prs=args.hide_clean_prs,
        executed_at=executed_at,
    )

    out_path = args.output
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"已生成报表: {out_path}")


if __name__ == "__main__":
    main()
