"""Mirror GitHub branches/tags and published release assets to Gitee."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


class SyncError(RuntimeError):
    pass


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: object, code: int,
                         msg: str, headers: object, newurl: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise SyncError("Refusing a download redirect without HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # API downloads can redirect to object storage; never forward the API token.
            redirected.remove_header("Authorization")
        return redirected


class Gitee:
    def __init__(self, owner: str, repo: str, token: str) -> None:
        self.base = f"https://gitee.com/api/v5/repos/{owner}/{repo}"
        self.token = token

    def request(self, path: str, method: str = "GET", data: dict | None = None,
                file: Path | None = None) -> object:
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if file is not None:
            # Stream uploads with curl; keep credentials off the command line.
            config = "header = " + json.dumps(f"Authorization: Bearer {self.token}") + "\n"
            result = subprocess.run(
                ["curl", "--config", "-", "--silent", "--show-error", "--fail",
                 "--connect-timeout", "20", "--max-time", "600", "--request", method,
                 "--form", f"file=@{file}", self.base + path],
                input=config, capture_output=True, text=True,
            )
            if result.returncode:
                reason = result.stderr.strip().replace(self.token, "***")
                raise SyncError(f"Gitee upload {file.name}: curl {result.returncode}: {reason}")
            return json.loads(result.stdout)
        elif data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.build_opener(DownloadRedirect()).open(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET" and path.startswith("/releases/tags/"):
                return None
            raise SyncError(f"Gitee {method} {path}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            reason = f"{type(exc.reason).__name__}: {exc.reason}".replace(self.token, "***")
            raise SyncError(f"Gitee {method} {path}: network request failed ({reason})") from None

    def attachments(self, release_id: int) -> list[dict]:
        result = []
        page = 1
        while True:
            batch = self.request(f"/releases/{release_id}/attach_files?per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def sync_code(source: str, target: str, folder: Path) -> None:
    git("init", "--bare", str(folder), cwd=folder.parent)
    git("fetch", "--no-tags", source, "+refs/heads/*:refs/heads/*",
        "+refs/tags/*:refs/tags/*", cwd=folder)
    refs = dict(line.split(" ", 1)[::-1] for line in git(
        "for-each-ref", "--format=%(objectname) %(refname)",
        "refs/heads/", "refs/tags/", cwd=folder).splitlines())
    if not refs:
        raise SyncError("GitHub source has no branches or tags")
    # Only local fetches force updates. Destination pushes never force or prune.
    git("push", target, *(f"{ref}:{ref}" for ref in refs), cwd=folder)
    remote = dict(line.split("\t", 1)[::-1] for line in git(
        "ls-remote", "--refs", target, cwd=folder).splitlines())
    if any(remote.get(ref) != sha for ref, sha in refs.items()):
        raise SyncError("Gitee branch/tag SHA verification failed")
    print(f"Verified {len(refs)} branch/tag references on Gitee", flush=True)


def download(url: str, destination: Path) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SyncError("Refusing a download without HTTPS")
    try:
        with urllib.request.build_opener(DownloadRedirect()).open(url, timeout=120) as response:
            with destination.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise SyncError(f"Download failed: {destination.name}") from None


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
        return digest.hexdigest()


def verify_packages(folder: Path, tag: str) -> dict[str, str]:
    manifest = folder / "SHA256SUMS.txt"
    expected = {}
    patterns = [
        rf"pr2resolve-{re.escape(tag)}-windows-x86_64\.zip",
        rf"pr2resolve-{re.escape(tag)}-macos-[A-Za-z0-9_]+\.tar\.gz",
        rf"pr2resolve-{re.escape(tag)}-linux-x86_64\.tar\.gz",
    ]
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)", line)
        if match is None or any(part in (".", "..") for part in match[2].split("/")):
            raise SyncError("Invalid path in SHA256SUMS.txt")
        # Older releases contain build-directory prefixes; preserve the manifest bytes.
        name = PurePosixPath(match[2]).name
        if name in expected:
            raise SyncError("Invalid or duplicate entry in SHA256SUMS.txt")
        expected[name] = match[1].lower()
    if len(expected) != 3 or any(
        sum(bool(re.fullmatch(pattern, name)) for name in expected) != 1 for pattern in patterns
    ):
        raise SyncError("Expected exactly one package for each of Windows, macOS and Linux")
    for name, digest in expected.items():
        if sha256(folder / name) != digest:
            raise SyncError(f"GitHub checksum mismatch: {name}")
    expected[manifest.name] = sha256(manifest)
    return expected


def sync_release(api: Gitee, source: dict, folder: Path, target_commit: str) -> None:
    if source["draft"] or source["prerelease"]:
        raise SyncError("Only published stable GitHub releases can be synced")
    tag = source["tag_name"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise SyncError("Release tag must match vMAJOR.MINOR.PATCH")
    assets = source["assets"]
    names = [asset["name"] for asset in assets]
    if len(names) != 4 or len(set(names)) != 4 or "SHA256SUMS.txt" not in names:
        raise SyncError("Expected three GitHub release packages and SHA256SUMS.txt")
    for asset in assets:
        name = asset["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SyncError("Unsafe release asset filename")
        download(asset["browser_download_url"], folder / name)
        if (folder / name).stat().st_size != asset["size"]:
            raise SyncError(f"GitHub asset size mismatch: {name}")
    expected = verify_packages(folder, tag)
    api.request("")  # Distinguish an inaccessible repository from an absent release.
    release = api.request("/releases/tags/" + urllib.parse.quote(tag, safe=""))
    metadata = {"tag_name": tag, "name": source["name"] or tag,
                "body": source["body"] or "", "prerelease": False}
    existing = {}
    if release is not None:
        for attachment in api.attachments(release["id"]):
            name = attachment["name"]
            if name in existing or name not in expected:
                raise SyncError(f"Unexpected or duplicate Gitee attachment: {name}")
            existing[name] = attachment
            downloaded = folder / ("gitee-" + name)
            download(attachment["browser_download_url"], downloaded)
            if sha256(downloaded) != expected[name]:
                raise SyncError(f"Gitee attachment conflict: {name}; refusing to overwrite")
    else:
        release = api.request("/releases", "POST", {**metadata, "target_commitish": target_commit})
    base = f'/releases/{release["id"]}/attach_files'
    for name in sorted(expected, key=lambda name: (folder / name).stat().st_size):
        if name not in existing:
            print(f"Uploading {name} ({(folder / name).stat().st_size} bytes)", flush=True)
            api.request(base, "POST", file=folder / name)
    attachments = api.attachments(release["id"])
    if sorted(a["name"] for a in attachments) != sorted(expected):
        raise SyncError("Gitee attachment list verification failed")
    for attachment in attachments:
        downloaded = folder / ("verified-" + attachment["name"])
        download(attachment["browser_download_url"], downloaded)
        if sha256(downloaded) != expected[attachment["name"]]:
            raise SyncError(f'Gitee upload checksum mismatch: {attachment["name"]}')
    if any(release.get(key) != value for key, value in metadata.items()):
        api.request(f'/releases/{release["id"]}', "PATCH", metadata)
    print(f"Verified Gitee release {tag}: three packages and SHA256SUMS.txt")


def main() -> int:
    owner = os.environ.get("GITEE_OWNER", "")
    repo = os.environ.get("GITEE_REPO", "")
    github_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in (owner, repo)):
        raise SyncError("Set valid GITEE_OWNER and GITEE_REPO variables")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repo):
        raise SyncError("Set GITHUB_REPOSITORY to owner/repo")
    tag = os.environ.get("RELEASE_TAG", "")
    token = os.environ.get("GITEE_TOKEN", "")
    if tag and not token:
        raise SyncError("Set the GITEE_TOKEN secret to sync releases")
    with tempfile.TemporaryDirectory(prefix="gitee-sync-") as temp:
        folder = Path(temp)
        bare = folder / "source.git"
        sync_code(f"https://github.com/{github_repo}.git", f"git@gitee.com:{owner}/{repo}.git", bare)
        if tag:
            endpoint = "latest" if tag == "latest" else "tags/" + urllib.parse.quote(tag, safe="")
            source = json.loads(subprocess.check_output(
                ["gh", "api", f"repos/{github_repo}/releases/{endpoint}"], text=True, encoding="utf-8"))
            commit = git("rev-parse", "--verify", f'refs/tags/{source["tag_name"]}^{{commit}}', cwd=bare)
            sync_release(Gitee(owner, repo, token), source, folder, commit)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        raise SystemExit(f"ERROR: {error}") from None
