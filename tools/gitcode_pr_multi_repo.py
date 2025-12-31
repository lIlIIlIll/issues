#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
import sys
import time
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
    states: List[str]  # 支持多个状态
    per_page: int


@dataclass
class Config:
    access_token: Optional[str]
    users: List[str]
    repos: List[RepoConfig]
    max_pr_pages: Optional[int] = None


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
    从 dict 里读出状态列表，支持:
      state = "open"
      states = ["open", "merged"]
    都归一化成 List[str]，否则用 default_states。
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


def _normalize_max_pages(raw: Any, default: Optional[int]) -> Optional[int]:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return None
    return value


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

    # 全局默认：允许 state / states 两种写法，默认 ["all"]
    global_states = _normalize_states(data, ["all"])

    global_per_page = int(data.get("per_page", 50))
    if global_per_page < 1 or global_per_page > 100:
        global_per_page = 50

    max_pr_pages = _normalize_max_pages(data.get("max_pr_pages"), None)

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

    return Config(
        access_token=access_token,
        users=users,
        repos=repos,
        max_pr_pages=max_pr_pages,
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


def fetch_prs_for_user(
    access_token: Optional[str],
    repo_cfg: RepoConfig,
    username: str,
    max_pages: Optional[int] = None,
) -> List[PRInfo]:
    """
    支持多个状态：
    对 repo_cfg.states 里的每个 state 分别请求一轮，再按 PR number 去重。
    """
    all_prs: List[PRInfo] = []
    seen_numbers: set[int] = set()

    for state in repo_cfg.states:
        page = 1
        while True:
            if max_pages is not None and page > max_pages:
                break
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
                num = int(pr["number"])
                if num in seen_numbers:
                    continue
                seen_numbers.add(num)

                head = pr.get("head") or {}
                base = pr.get("base") or {}

                all_prs.append(
                    PRInfo(
                        number=num,
                        title=pr.get("title", ""),
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
            time.sleep(0.1)

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


# ----------------- 输出 -----------------


def print_report_for_user(
    repo_cfg: RepoConfig,
    username: str,
    prs: List[PRInfo],
    *,
    only_unresolved: bool,
    hide_clean_prs: bool,
) -> None:
    # 这里先根据 hide_clean_prs 把“干净 PR”滤掉
    if hide_clean_prs:
        visible_prs: List[PRInfo] = []
        for pr in prs:
            has_unresolved = any((cm.resolved is False) for cm in pr.comments)
            if has_unresolved:
                visible_prs.append(pr)
    else:
        visible_prs = prs

    print()
    print("#" * 80)
    print(
        f"仓库: {repo_cfg.owner}/{repo_cfg.repo} | 用户: {username}  —— PR 数量: {len(visible_prs)}"
    )
    if only_unresolved:
        print("(仅统计未解决的检视意见)")
    if hide_clean_prs:
        print("(已隐藏没有未解决检视意见的 PR)")
    print("#" * 80)

    if not visible_prs:
        print("（没有符合条件的 PR）")
        return

    for pr in visible_prs:
        print(f"- PR #{pr.number} [{pr.state}] {pr.title}")
        print(f"  URL     : {pr.html_url}")

        if pr.source_branch or pr.target_branch:
            if pr.target_branch:
                print(f"  Branch  : {pr.source_branch} -> {pr.target_branch}")
            else:
                print(f"  Branch  : {pr.source_branch}")

        line = f"  Created : {pr.created_at}"
        if pr.updated_at:
            line += f"  |  Updated: {pr.updated_at}"
        print(line)

        if pr.merged_at:
            print(f"  Merged  : {pr.merged_at}")

        # Issues 保持不变
        if not pr.issues:
            print("  Issues  : （无关联 Issue）")
        else:
            print("  Issues  :")
            for iss in pr.issues:
                labels_str = f" labels={','.join(iss.labels)}" if iss.labels else ""
                print(f"    - #{iss.number} [{iss.state}] {iss.title}{labels_str}")
                print(f"      {iss.url}")

        # 🔴 先算所有未解决评论（resolved == False）
        unresolved_comments = [cm for cm in pr.comments if cm.resolved is False]

        # 然后根据 only_unresolved 决定实际展示的评论集合
        if only_unresolved:
            filtered_comments = unresolved_comments
        else:
            # 默认模式：有 resolved 状态的都展示（True/False）
            filtered_comments = [cm for cm in pr.comments if cm.resolved is not None]

        if not filtered_comments:
            if only_unresolved:
                # 在 hide_clean_prs=true 的情况下，这种分支理论上不会出现，
                # 因为没未解决评论的 PR 前面已经被过滤掉了。
                print("  Reviews : （无未解决的检视意见）")
            else:
                print("  Reviews : （无需要 resolved 状态的检视意见）")
        else:
            if only_unresolved:
                print(f"  Reviews : 共 {len(filtered_comments)} 条未解决检视意见")
            else:
                print(
                    f"  Reviews : 共 {len(filtered_comments)} 条（仅显示带 resolved 状态的）"
                )

            for cm in filtered_comments:
                resolved_str = "resolved" if cm.resolved else "unresolved"

                loc = ""
                if cm.path:
                    loc = f" ({cm.path}"
                    if cm.position is not None:
                        loc += f":{cm.position}"
                    loc += ")"

                print(f"    - [#{cm.id}] [{resolved_str}] {cm.user}{loc}")
                body_lines = (cm.body or "").splitlines() or [""]
                for line in body_lines:
                    print(f"        {line}")
                print(f"        created_at={cm.created_at}, updated_at={cm.updated_at}")
                print()

        print()  # 空一行分隔 PR


# ----------------- main -----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="统计 GitCode 多个仓库中指定用户的 PR，列出关联 Issue 和检视意见（评论）"
    )
    parser.add_argument(
        "-c",
        "--config",
        default="gitcode_pr_config.toml",
        help="配置文件路径（默认 gitcode_pr_config.toml）",
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

    for repo_cfg in cfg.repos:
        for username in cfg.users:
            try:
                prs = fetch_prs_for_user(
                    cfg.access_token,
                    repo_cfg,
                    username,
                    max_pages=cfg.max_pr_pages,
                )
                for pr in prs:
                    pr.issues = fetch_issues_for_pr(
                        cfg.access_token, repo_cfg, pr.number
                    )
                    pr.comments = fetch_comments_for_pr(
                        cfg.access_token, repo_cfg, pr.number
                    )
                print_report_for_user(
                    repo_cfg,
                    username,
                    prs,
                    only_unresolved=args.only_unresolved,
                    hide_clean_prs=args.hide_clean_prs,
                )
            except Exception as e:
                print(
                    f"\n!!! 获取 {repo_cfg.owner}/{repo_cfg.repo} 中 {username} 的 PR 时出错: {e}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
