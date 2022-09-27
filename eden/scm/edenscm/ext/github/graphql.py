# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

"""make calls to GitHub's GraphQL API
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from sys import platform
from typing import Optional

from bindings import github
from edenscm import pycompat


@dataclass
class GitHubPullRequest:
    # In GitHub, a "RepositoryOwner" is either an "Organization" or a "User":
    # https://docs.github.com/en/graphql/reference/interfaces#repositoryowner
    repo_owner: str
    repo_name: str
    number: int

    def as_url(self, domain=None) -> str:
        domain = domain or "github.com"
        return f"https://{domain}/{self.repo_owner}/{self.repo_name}/pull/{self.number}"


def get_pull_request_data(token: str, pr: GitHubPullRequest):
    return github.get_pull_request(token, pr.repo_owner, pr.repo_name, pr.number)


def get_github_oauth_token() -> Optional[str]:
    oauth_token = try_parse_oauth_token_from_gh_config()
    if oauth_token:
        return oauth_token

    # Fallback: try reading the OAuth token from .ghstackrc.
    # This is a simplified version of the logic ghstack uses to read its own
    # config file:
    # https://github.com/ezyang/ghstack/blob/master/ghstack/config.py
    return try_parse_oauth_token_from_ghstack()


def try_parse_oauth_token_from_gh_config() -> Optional[str]:
    """This function is very defensive, as we do not want to throw an exception
    if we cannot extract an OAuth token. We leave it to the caller to decide
    whether a missing OAuth token needs to be communicated to the user.
    """
    if platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        hosts_yml = Path(appdata) / "GitHub CLI" / "hosts.yml"
    else:
        home = os.environ.get("HOME")
        if not home:
            return None
        hosts_yml = Path(home) / ".config" / "gh" / "hosts.yml"

    try:
        with open(hosts_yml, "r") as f:
            contents = f.read()
    except OSError:
        return None

    return try_parse_oath_token_from_hosts_yml(contents)


def try_parse_oath_token_from_hosts_yml(contents: str) -> Optional[str]:
    r"""Because we do not want to incur the cost of a third-party YAML parser,
    we exploit the fact that, in practice, we expect hosts.yml to be formatted
    in a simple way that we can parse using regular expressions.

    >>> try_parse_oath_token_from_hosts_yml("") is None
    True
    >>> try_parse_oath_token_from_hosts_yml('''
    ... github.com:
    ...     oauth_token: ListTheTokenFirst
    ...     user: bolinfest
    ...     git_protocol: https
    ... ''')
    'ListTheTokenFirst'
    >>> try_parse_oath_token_from_hosts_yml('''
    ... github.com:
    ...     user: bolinfest
    ...     oauth_token: ListTheTokenSecond
    ...     git_protocol: https
    ... ''')
    'ListTheTokenSecond'
    """
    username = None
    token = None
    in_github_dot_com_section = False

    for line in re.split(r"\r?\n", contents):
        if in_github_dot_com_section:
            match = re.match(r"^\s+(user|oauth_token):\s*(\S+)$", line)
            if match:
                key = match.group(1)
                if key == "user" and not username:
                    username = match.group(2)
                elif key == "oauth_token" and not token:
                    token = match.group(2)
                if token and username:
                    return token
            elif not line and re.match(r"^\S", line):
                # Must be the start of a new section.
                in_github_dot_com_section = False
        elif line == "github.com:":
            in_github_dot_com_section = True
    return None


def try_parse_oauth_token_from_ghstack() -> Optional[str]:
    current_dir = Path(pycompat.getcwd())

    while current_dir != Path("/"):
        config_path = "/".join([str(current_dir), ".ghstackrc"])
        token = try_parse_oauth_token_from_ghstackrc(config_path)
        if token:
            return token
        current_dir = current_dir.parent

    # If this is used in a /tmp folder, then ~/.ghstackrc will not be an
    # ancestor of getcwd(), but it should be considered, anyway.
    config_path = os.path.expanduser("~/.ghstackrc")
    return try_parse_oauth_token_from_ghstackrc(config_path)


def try_parse_oauth_token_from_ghstackrc(config_path: str) -> Optional[str]:
    import configparser

    config = configparser.ConfigParser()
    try:
        with open(config_path) as f:
            config.read_file(f)
            token = config.get("ghstack", "github_oauth")
            if token:
                return token
    except Exception:
        # Could be FileNotFoundError, a parse error...just ignore.
        pass
    return None
